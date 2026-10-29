"""Live mode's interactive shell: a real local Python session (LocalSession,
the same engine API client mode uses) with a remote Home Assistant session
alongside it.

Code runs here, in this process, by default - `import polars`, define
functions, keep a dataframe around; none of it touches Home Assistant. `sql`
and `hass_api` are local objects that fetch data over the wire (Arrow over
the websocket, and the REST API) and hand back local results.

`hass` and `obj` are the exception: they only exist inside Home Assistant,
so a statement that uses either one (or a variable an earlier such statement
left behind over there) is sent to the server-side session and run there,
with its output shipped back as text - see LiveSession for exactly how
that's decided and what crosses over. `ha-repl exec` runs its snippet the
same way (cli.py), just without the prompt.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import copy
import io
import json
import sys
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from .cli import display_options, print_result
from .client import Client, HaReplError, client_version
from .complete import context_start
from .completer import HaCompleter, names_read
from .config import display_path
from .editor import python_prompt, run_interruptibly
from .local_session import LocalSession, format_error
from .plugins import bind_context, bind_rebind, run_plugins
from .render import decode_value
from .rest import hass_api
from .sql import SqlResult, SqlRow, SqlTool

HISTORY = Path.home() / ".ha_repl_history"

# Names that only ever exist on the Home Assistant side.
REMOTE_ONLY = frozenset({"hass", "obj"})

# Local values bigger than this (as source text) aren't copied to the server.
_MAX_SHIPPED_CHARS = 100_000

_LIST_ITERATOR: type = type(iter([]))

_LITERAL_TYPES = (
    str,
    bytes,
    int,
    float,
    bool,
    type(None),
    list,
    tuple,
    dict,
    set,
)


@dataclass
class Captured:
    """What a run produced, collected instead of printed - for `ha-repl exec
    --json`. `value` is the trailing expression's, as JSON-ready data."""

    stdout: str = ""
    value: Any = None
    error: dict[str, str] | None = None
    # Server-side output was cut short at the server's size limit.
    truncated: bool = False


@dataclass
class _Chunk:
    """Consecutive statements that run on the same side."""

    remote: bool
    start: int  # first source line, 1-based
    end: int  # last source line, inclusive
    names: _Names
    count: int = 1  # statements


@dataclass
class LiveSession:
    """Decides, one statement at a time, which side runs it.

    A statement runs on the server if it reads a name that lives there:
    `hass`, `obj`, or a variable an earlier server-side statement assigned
    (`s = hass.states.get("sun.sun")`, then `s.state`). Everything else
    runs locally. A name lives wherever it was last assigned.

    The two namespaces are otherwise separate, and only plain data
    (strings, numbers, lists/dicts of them) crosses between them. Local ->
    server, for a server-side statement that uses local names: plain data
    is copied over, and an imported module is imported there under the same
    name. Server -> local: a variable a server-side statement assigns comes
    back, and is local from then on, if what it holds is plain data. Anything
    else local (a dataframe, a function, `sql` or `hass_api` themselves)
    can't follow, and the statement is refused with an explanation rather
    than failing over there with a puzzling NameError.
    """

    client: Any
    session_name: str
    local: LocalSession
    auto_await: bool = True
    remote_names: set[str] = field(default_factory=set)
    # Bound locally by this shell itself (`sql`, `hass_api`) - never copied over.
    shared: frozenset[str] = frozenset()
    # Set to collect output instead of printing it.
    capture: Captured | None = None
    # Seconds the server allows each of its own statements, if any.
    timeout: float | None = None
    # A statement is running in Home Assistant now.
    _remote_busy: bool = field(default=False, init=False)
    _sends: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    async def refresh_remote_names(self) -> None:
        """Pick up what an existing server session already holds (it
        outlives this process); after that, run() keeps track itself."""
        reply = await self.client.call("ha_repl_server/sessions")
        self.remote_names = set()
        for session in reply["sessions"]:
            if session["name"] == self.session_name:
                self.remote_names = set(session["variables"]) - self._local_names()

    async def run(self, source: str, *, echo: bool = True) -> bool:
        """Run source, stopping at the first statement that fails. Only the
        final statement's value is echoed, and not even that with `echo`
        off. Returns False on any failure."""
        try:
            tree = ast.parse(source)
        except SyntaxError, ValueError:
            return await self._run_local(source, echo=echo)  # reports the error
        if not tree.body:
            return await self._run_local(source, echo=echo)
        lines = source.splitlines()
        body = tree.body
        whole = True
        while body:
            # Planned one step at a time: where the rest runs depends on
            # what this step leaves behind, and on which side.
            chunk = self._next_chunk(body)
            body = body[chunk.count :]
            # Untouched if it's all there is, so comments and layout survive
            # into tracebacks.
            text = (
                source
                if whole and not body
                else "\n".join(lines[chunk.start - 1 : chunk.end])
            )
            whole = False
            if chunk.remote:
                ok = await self._run_remote(text, chunk.names, echo=echo and not body)
            else:
                ok = await self._run_local(text, echo=echo and not body)
                self.remote_names -= chunk.names.assigned
            if not ok:
                return False
        return True

    async def run_quiet(self, source: str) -> dict[str, Any] | None:
        """Run source the way a plugin's statement is run: no value
        echoed, what it prints sent to stderr, and its error (None if it
        worked) handed back rather than reported."""
        outer, quiet = self.capture, Captured()
        self.capture = quiet
        try:
            await self.run(source, echo=False)
        finally:
            self.capture = outer
        sys.stderr.write(quiet.stdout)
        return quiet.error

    def _next_chunk(self, body: list[ast.stmt]) -> _Chunk:
        """The leading statements of `body` that run together: a run of
        local ones, or a single server-side one - what a server-side
        statement assigns may come back as local data (see _run_remote),
        which changes where the statements after it belong."""
        local = self._local_names()
        remote = (REMOTE_ONLY | self.remote_names) - local
        chunk: _Chunk | None = None
        for stmt in body:
            names = _Names()
            names.visit(stmt)
            is_remote = bool(names.loaded & remote)
            start = min([
                stmt.lineno,
                *(d.lineno for d in getattr(stmt, "decorator_list", [])),
            ])
            end = stmt.end_lineno or stmt.lineno
            if chunk is None:
                chunk = _Chunk(is_remote, start, end, names)
            elif start <= chunk.end:
                # Two statements sharing a line (`a = 1; hass.x`) can't be cut
                # apart by line, so they go together - to the server if either does.
                chunk.remote = chunk.remote or is_remote
                chunk.end = max(chunk.end, end)
                chunk.names.merge(names)
                chunk.count += 1
            elif not chunk.remote and not is_remote:
                chunk.end = end
                chunk.names.merge(names)
                chunk.count += 1
            else:
                break
            local |= names.assigned
            remote -= names.assigned
        assert chunk is not None
        return chunk

    async def _run_local(self, source: str, *, echo: bool) -> bool:
        if self.capture is None:
            ok = await self.local.run(source, echo=echo)
        else:
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    value = await self.local.evaluate(source, echo=echo)
            except asyncio.CancelledError:
                raise
            except BaseException as err:  # noqa: BLE001 - report everything, incl. SystemExit
                self.capture.error = format_error(err)
                ok = False
            else:
                if echo and value is not None:
                    self.capture.value = _jsonable(value)
                ok = True
            finally:
                self.capture.stdout += out.getvalue()
        if "_" in self.local.globals_:
            self.remote_names.discard("_")
        return ok

    async def _run_remote(self, source: str, names: _Names, *, echo: bool) -> bool:
        local_names = self._local_names()
        via = ", ".join(
            f"`{n}`" for n in sorted(names.loaded & (REMOTE_ONLY | self.remote_names))
        )
        local_only = sorted(names.loaded & self.shared & local_names)
        if local_only:
            self._refuse(
                f"not run - {', '.join(f'`{n}`' for n in local_only)} only "
                f"work{'s' if len(local_only) == 1 else ''} locally and {via} only "
                "inside Home Assistant, so one statement can't use both. Assign "
                "the Home Assistant part to a variable in a statement of its own "
                "first - plain data (strings, numbers, lists/dicts of them) comes back."
            )
            return False
        preamble: list[str] = []
        stuck: list[str] = []
        for name in sorted(names.loaded & (local_names - self.shared)):
            line = _ship(name, self.local.globals_[name])
            if line is not None:
                preamble.append(line)
            elif name not in names.bound:
                stuck.append(name)
        if stuck:
            self._refuse(
                f"not run - this would run inside Home Assistant (it uses {via}), but "
                f"{', '.join(f'`{n}`' for n in stuck)} only "
                f"exist{'s' if len(stuck) == 1 else ''} in the local session and can't "
                "be copied over (only plain data, small sql results and imported modules can)."
            )
            return False
        if preamble:
            copied = await self._exec_remote("\n".join(preamble), echo=False)
            if copied is None:
                return False
        # What it assigns lives on the server afterwards - except plain data,
        # which the server hands back and is local from here on.
        returned = await self._exec_remote(
            source, echo=echo, fetch=sorted(names.assigned)
        )
        ok = returned is not None
        for name in names.assigned:
            self.local.globals_.pop(name, None)
        self.remote_names |= names.assigned
        for name, tree in (returned or {}).items():
            self.local.globals_[name] = decode_value(tree)
            self.remote_names.discard(name)
        if echo:
            # `_` is whichever side answered last.
            self.local.globals_.pop("_", None)
            self.remote_names.add("_")
        return ok

    async def _exec_remote(
        self, source: str, *, echo: bool, fetch: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Run source in the server-side session. None if it raised;
        otherwise those of the `fetch` variables that came back as data."""
        payload: dict[str, Any] = {
            "code": source,
            "session": self.session_name,
            "auto_await": self.auto_await,
            **display_options(),
        }
        if self.timeout:
            payload["timeout"] = self.timeout
        if fetch:
            payload["fetch"] = fetch
        if self.capture is not None:
            # Escape codes inside a JSON string are just noise for a consumer
            # that asked for machine-readable output.
            payload["color"] = False
        self._remote_busy = True
        try:
            result = await self.client.call("ha_repl_server/exec", **payload)
        finally:
            self._remote_busy = False
        if not echo:
            result = {**result, "value": None}
        if self.capture is None:
            print_result(result)
        else:
            if result["stdout"] is not None:
                self.capture.stdout += result["stdout"]
            tree = result.get("value_tree")
            # What show() made is already text laid out to be read.
            as_text = tree is None or result.get("shown")
            self.capture.value = (
                result["value"] if as_text else _jsonable(decode_value(tree))
            )
            # The frames are for drawing a traceback, not for a JSON consumer.
            self.capture.error = result["error"] and {
                k: v for k, v in result["error"].items() if k != "stacks"
            }
            self.capture.truncated |= bool(result.get("truncated"))
        return None if result["error"] else result.get("names") or {}

    def interrupt(self) -> bool:
        """Ctrl-C: if a statement is running in Home Assistant, ask the
        server to stop it there, and say so. Its exec then answers as
        usual, with where it stopped."""
        if not self._remote_busy:
            return False
        send = asyncio.ensure_future(
            self.client.send("ha_repl_server/cancel", session=self.session_name)
        )
        self._sends.add(send)
        send.add_done_callback(self._sends.discard)
        return True

    def _refuse(self, message: str) -> None:
        if self.capture is None:
            print(f"ha-repl: {message}", file=sys.stderr)
        else:
            self.capture.error = {
                "type": "HaReplError",
                "message": message,
                "traceback": f"ha-repl: {message}\n",
            }

    def remote_names_now(self) -> set[str]:
        """The names that live in Home Assistant."""
        return {*REMOTE_ONLY, *self.remote_names}

    def completes_remotely(self, text: str) -> bool:
        """Whether what's being typed at the end of `text` belongs to Home
        Assistant: the input so far reads something that lives there, as
        run() would decide - leaving out the word still being typed."""
        return bool(names_read(text[: context_start(text)]) & self.remote_names_now())

    def _local_names(self) -> set[str]:
        return {
            name
            for name in self.local.globals_
            if not name.startswith("__")
            # the shell's own, with a counterpart inside Home Assistant
            and name not in ("_maybe_await", "unawait", "help", "show")
        }


class _Names(ast.NodeVisitor):
    """The names a piece of code reads (`loaded`), the ones it binds
    anywhere at all (`bound` - including arguments and comprehension
    variables), and the ones it leaves assigned in the session's own
    namespace afterwards (`assigned` - not those inner-scope ones)."""

    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.bound: set[str] = set()
        self.assigned: set[str] = set()
        self._depth = 0

    def merge(self, other: _Names) -> None:
        self.loaded |= other.loaded
        self.bound |= other.bound
        self.assigned |= other.assigned

    def _bind(self, name: str) -> None:
        self.bound.add(name)
        if self._depth == 0:
            self.assigned.add(name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.loaded.add(node.id)
        else:
            self._bind(node.id)

    def visit_alias(self, node: ast.alias) -> None:
        if node.name != "*":
            self._bind((node.asname or node.name).partition(".")[0])

    def visit_arg(self, node: ast.arg) -> None:
        self.bound.add(node.arg)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # Annotations aren't read: nothing evaluates them when the statement
        # runs (PEP 649), so a type that only exists on one side - `Any`
        # imported locally, a class from the component - is no obstacle.
        if node.value is not None:
            self.visit(node.target)
            self.visit(node.value)
        elif not isinstance(node.target, ast.Name):
            self.visit(node.target)
        # else a bare `hass: HomeAssistant`, which binds nothing

    def _visit_def(self, node: Any) -> None:
        self._bind(node.name)
        if getattr(node, "returns", None) is not None:
            node = copy.copy(node)
            node.returns = None
        self._visit_scope(node)

    def _visit_scope(self, node: ast.AST) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_def
    visit_Lambda = visit_ListComp = visit_SetComp = visit_DictComp = _visit_scope
    visit_GeneratorExp = _visit_scope


def _jsonable(value: Any) -> Any:
    """A trailing value as something json.dumps accepts: a SqlResult as
    its rows, anything already JSON-shaped as itself, a rich renderable
    (the Table show() returns) as plain text, the rest as their repr."""
    if isinstance(value, SqlResult):
        return value.json_data()
    if isinstance(value, SqlRow):
        return value.json_data()
    if not isinstance(value, type) and (
        hasattr(value, "__rich__") or hasattr(value, "__rich_console__")
    ):
        out = io.StringIO()
        Console(file=out, color_system=None, width=120).print(value)
        return out.getvalue()
    try:
        json.dumps(value)
    except TypeError, ValueError:
        return repr(value)
    return value


def _ship(name: str, value: Any) -> str | None:
    """A line of source that recreates this local value under the same name
    on the server, or None if it isn't something that can be recreated."""
    if isinstance(value, types.ModuleType):
        return f"import {value.__name__} as {name}"
    if isinstance(value, SqlResult):
        # Crosses as its rows, a list of lists - enough for the loop or
        # comprehension over a result that calls into `hass` per row.
        value = list(value)
    if isinstance(value, SqlRow):
        value = value.values
    template = "{}"
    if type(value) is _LIST_ITERATOR:
        # What obj.find_names()/find_paths() hand back: recreated from the
        # items it has left, read from a copy so this one isn't used up.
        remaining: Any = copy.copy(value)
        value, template = list(remaining), "iter({})"
    if not isinstance(value, _LITERAL_TYPES):
        return None
    try:
        text = repr(value)
        if len(text) > _MAX_SHIPPED_CHARS or ast.literal_eval(text) != value:
            return None
    except Exception:  # noqa: BLE001 - any failure just means "can't be copied"
        return None
    return f"{name} = {template.format(text)}"


async def connect_live(
    client: Client,
    session_name: str,
    *,
    auto_await: bool = True,
    capture: Captured | None = None,
    timeout: float | None = None,
) -> LiveSession:
    """A LiveSession with the local `sql` and `hass_api` bound - shared by
    the interactive shell and `ha-repl exec`, so a snippet behaves the
    same in either."""
    bindings: dict[str, Any] = {}
    try:
        bindings["hass_api"] = await hass_api(client.url, client.token)
    except HaReplError as err:
        print(f"ha-repl: hass_api unavailable: {err}", file=sys.stderr)
        bindings["hass_api"] = None
    try:
        bindings["sql"] = await SqlTool.connect(client)
    except HaReplError as err:
        # Switched off in the integration's options - left unbound.
        print(f"ha-repl: sql unavailable: {err}", file=sys.stderr)

    live = LiveSession(
        client,
        session_name,
        LocalSession(dict(bindings), auto_await=auto_await),
        auto_await=auto_await,
        shared=frozenset(bindings),
        capture=capture,
        timeout=timeout,
    )
    await live.refresh_remote_names()
    return live


def bind_live_rebind(live: LiveSession, plugins: Sequence[Path]) -> None:
    """Bind `rebind()` in a live session - once everything else of the
    shell's own is bound there, and before the plugins first run."""
    bind_rebind(live.local.globals_, plugins, live.run_quiet, REMOTE_ONLY)
    live.shared |= {"rebind"}


async def run_repl(
    client: Client,
    session_name: str,
    *,
    auto_await: bool = True,
    server_name: str | None = None,
    plugins: Sequence[Path] = (),
) -> int:
    try:
        info = await client.call("ha_repl_server/info")
        server = f"Live Server v{info['version']}"
    except HaReplError:
        # An older server predating this command - not fatal, just less to show.
        info, server = {}, "Live Server"

    live = await connect_live(client, session_name, auto_await=auto_await)
    completer = HaCompleter(live.local.globals_, live)
    completer.remote = "complete" in info.get("commands", ())
    prompt = python_prompt(HISTORY, completer)
    bind_context(live.local.globals_, "live", server_name)
    bind_live_rebind(live, plugins)
    loaded = await run_plugins(plugins, live.run_quiet)
    print(
        f"Live client v{client_version()} connected to {server} at HA API "
        f"{banner_address(client.url, server_name)} (session {session_name!r}). "
        "Ctrl-D to exit.\n"
        "Python runs locally; anything using `hass` or `obj` runs inside Home Assistant."
        f"{banner_plugins(loaded)}"
        + (
            ""
            if completer.remote
            else "\nCompletion inside Home Assistant needs Live Server v0.14.0 or later."
        )
    )
    while True:
        try:
            source = await prompt.prompt_async(">>> ")
        except KeyboardInterrupt:
            continue
        except EOFError:
            return 0
        if not source.strip():
            continue
        if is_quit_call(source):
            return 0
        await run_interruptibly(live.run(source), live.interrupt)
        completer.forget()


def banner_address(url: str, server_name: str | None) -> str:
    """Where a shell is connected, by name too if it was chosen by name."""
    return f"{server_name} ({url})" if server_name else url


def banner_plugins(loaded: Sequence[Path]) -> str:
    """The banner's line listing the plugins run, if there were any."""
    if not loaded:
        return ""
    return "\nPlugins: " + ", ".join(display_path(path) for path in loaded)


def is_quit_call(source: str) -> bool:
    """True for a bare `quit()` or `exit()` - the standard way to leave a REPL.

    Running that would raise SystemExit inside the session, caught and
    reported back as a confusing traceback instead of actually exiting
    anything, so it's handled like Ctrl-D instead.
    """
    try:
        (stmt,) = ast.parse(source).body
    except SyntaxError, ValueError:
        return False
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id in ("quit", "exit")
        and not stmt.value.args
        and not stmt.value.keywords
    )
