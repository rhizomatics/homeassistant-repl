"""REPL sessions: persistent namespaces that execute code on the running event loop.

Deliberately free of Home Assistant imports so it can be tested standalone.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import collections
import copy
import dataclasses
import inspect
import io
import itertools
import linecache
import math
import pprint
import pydoc
import re
import reprlib
import sys
import textwrap
import time
import traceback
import typing
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pygments import format as format_tokens
from pygments import lex
from pygments.formatters.terminal import TerminalFormatter
from pygments.lexers.python import PythonLexer

MAX_OUTPUT_CHARS = 1_000_000

# Variables bigger than this (as a tree) aren't handed back to the client.
MAX_FETCHED_CHARS = 100_000

# Source lines sent either side of each traceback frame's own.
_CONTEXT_LINES = 3
# Frames kept per exception; the middle of anything longer (a runaway
# recursion, typically) is dropped.
_MAX_FRAMES = 100

_cell_counter = itertools.count(1)

# help() on a hass object documents its *type*; pydoc has no docstrings for core HA
# classes written with newcomers in mind, so point at the real docs instead. Keyed by
# fully-qualified class name so it applies no matter what name the object is bound to.
# This list only grows, and is useful independently of the code around it, hence the
# data file rather than a dict literal here.
_DOC_URLS: dict[str, str] = yaml.safe_load(
    (Path(__file__).parent / "doc_urls.yaml").read_text()
)


@dataclass
class ExecResult:
    """Outcome of running one snippet."""

    stdout: str = ""
    # The trailing expression's value twice over: as plain text, and (unless
    # it's too big to be worth shipping) as the tree _encode_value() builds
    # for the client to pretty-print itself.
    value: str | None = None
    value_tree: Any = None
    # The value is a picture show() made, so a client wanting data rather
    # than a drawing is better served by `value`'s text than by the tree.
    shown: bool = False
    # The variables the caller asked after that turned out to be plain data,
    # each as a tree like value_tree.
    names: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    duration: float = 0.0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "value": self.value,
            "value_tree": self.value_tree,
            "shown": self.shown,
            "names": self.names,
            "error": self.error,
            "duration": self.duration,
            "truncated": self.truncated,
        }

    def summary(self, limit: int = 200) -> str:
        """One line saying how a run went, for the log."""
        if self.error is not None:
            outcome = f"raised {self.error['type']}: {self.error['message']}"
        elif self.value is not None:
            outcome = f"returned {self.value}"
        else:
            outcome = "finished"
        outcome = reprlib.Repr(maxstring=limit).repr(outcome)[1:-1]
        output = f", {len(self.stdout)} chars of output" if self.stdout else ""
        cut = ", truncated" if self.truncated else ""
        return f"{outcome} in {self.duration:.3f}s{output}{cut}"


@dataclass
class Session:
    """A named namespace that persists between executions until reset."""

    name: str
    globals_: dict[str, Any]
    protected: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    executions: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _running: asyncio.Future[Any] | None = None

    @property
    def busy(self) -> bool:
        return self._running is not None and not self._running.done()

    def cancel(self, reason: str) -> bool:
        """Stop the code running now, if any - reported to whoever ran it as
        a CancelledError carrying `reason`. False if nothing was running."""
        if not self.busy:
            return False
        assert self._running is not None
        return self._running.cancel(reason)

    async def run(
        self,
        source: str,
        timeout: float | None = None,
        *,
        color: bool = False,
        width: int = 88,
        auto_await: bool = True,
        fetch: Iterable[str] = (),
    ) -> ExecResult:
        """Execute source in this session, returning captured output and the
        last value - and, of the variables named in `fetch`, those that hold
        plain data afterwards."""
        async with self._lock:
            self.last_used = time.time()
            self.executions += 1
            out = io.StringIO()
            # hass/obj/open are re-seeded every run, not just at session creation, so a
            # snippet that does `obj = obj["/mqtt"]["..."]` only shadows them for its own
            # run - the next command always starts from the real bindings again.
            self.globals_.update(self.protected)
            self.globals_["print"] = _capturing_print(out)
            self.globals_["help"] = _capturing_help(out, color=color, width=width)
            self.globals_["show"] = show
            self.globals_["_maybe_await"] = _maybe_await
            self.globals_["unawait"] = unawait
            result = ExecResult()
            start = time.perf_counter()
            # Its own task, so cancel() can stop it without cancelling the caller.
            self._running = asyncio.ensure_future(
                _outcome(self._execute(source, auto_await=auto_await))
            )
            try:
                async with asyncio.timeout(timeout):
                    value, raised = await self._running
                if raised is not None:
                    raise raised
                if value is not None:
                    self.globals_["_"] = value
                    result.value = _plain_text(value, width=width)
                    result.value_tree = _encode_value(value)
                    result.shown = type(value) is Shown
            except asyncio.CancelledError as err:
                # Cancellation of the caller must propagate, not be reported as
                # a result; cancel() stopping the code itself is reported.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                result.error = _format_error(err)
            except BaseException as err:  # noqa: BLE001 - report everything, incl. SystemExit
                result.error = _format_error(err)
            finally:
                self._running = None
                result.duration = time.perf_counter() - start
            result.stdout = out.getvalue()
            for name in fetch:
                if name in self.globals_ and name not in self.protected:
                    tree = _encode_plain(self.globals_[name])
                    if tree is not _NOT_PLAIN:
                        result.names[name] = tree
            for attr in ("stdout", "value"):
                text = getattr(result, attr)
                if text is not None and len(text) > MAX_OUTPUT_CHARS:
                    setattr(result, attr, text[:MAX_OUTPUT_CHARS])
                    result.truncated = True
            return result

    async def _execute(self, source: str, *, auto_await: bool = True) -> Any:
        # Named the way the client names its own cells (local_session.py).
        filename = f"/ha_repl/cell_{next(_cell_counter)}"
        # Register the source so tracebacks can show the offending lines.
        linecache.cache[filename] = (
            len(source),
            None,
            (source if source.endswith("\n") else source + "\n").splitlines(
                keepends=True
            ),
            filename,
        )
        tree = ast.parse(source, filename, "exec")
        if auto_await:
            tree = _AutoAwait().visit(tree)
            ast.fix_missing_locations(tree)

        # Like the interactive interpreter, echo the value of a trailing expression.
        last_expr = None
        last_stmt = tree.body[-1] if tree.body else None
        if isinstance(last_stmt, ast.Expr):
            tree.body.pop()
            last_expr = ast.Expression(last_stmt.value)

        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        # dont_inherit=True: compile() otherwise inherits this module's own
        # `from __future__ import annotations`, which would make every
        # annotation in the user's code a plain string - breaking the
        # FORWARDREF-based signature rendering in _format_signature below.
        if tree.body:
            await _run_code(
                compile(tree, filename, "exec", flags=flags, dont_inherit=True),
                self.globals_,
            )
        if last_expr is None:
            return None
        return await _run_code(
            compile(last_expr, filename, "eval", flags=flags, dont_inherit=True),
            self.globals_,
        )


def _plain_text(value: Any, *, width: int) -> str:
    """A value as plain text - what a client that can't use the tree
    _encode_value() builds (too big to send, or a client that predates it)
    shows instead, and what `ha-repl --json` reports."""
    try:
        return pprint.pformat(value, width=width, sort_dicts=False)
    except Exception as err:  # noqa: BLE001 - a broken __repr__ can raise anything
        return f"<repr-error {str(err)!r}>"


class _TooBig(Exception):
    """The value doesn't fit in MAX_OUTPUT_CHARS as a tree."""


def _encode_value(value: Any) -> Any:
    """A value as JSON the client can rebuild into something its own
    pretty-printer lays out the way it would the real thing - the objects
    themselves never leave this process, so this is as close as the far end
    gets. None, bools, strings and ordinary ints/floats are sent as
    themselves and a list as a JSON list; everything else is a `{"t": ...}`
    node: the other builtin containers by kind, an iterator over a list as
    the items it has left ("iter"), a dataclass, attrs instance or namedtuple
    as its class name and fields ("obj"), and whatever is left as just its
    repr() ("repr"). None when the value is too big to send
    this way - the caller's plain text then stands in for it.
    """
    try:
        return _encode(value, [MAX_OUTPUT_CHARS], set())
    except _TooBig, RecursionError:
        return None


_NOT_PLAIN: Any = object()


def _encode_plain(value: Any) -> Any:
    """The same tree, but only for a value that survives the trip whole -
    builtin containers (or an iterator over a list), strings, numbers and
    None, all the way down - and isn't too big to be worth sending.
    _NOT_PLAIN otherwise.
    """
    try:
        tree = _encode(value, [MAX_FETCHED_CHARS], set())
    except _TooBig, RecursionError:
        return _NOT_PLAIN
    return tree if _is_plain(tree) else _NOT_PLAIN


def _is_plain(tree: Any) -> bool:
    if isinstance(tree, list):
        return all(_is_plain(item) for item in tree)
    if not isinstance(tree, dict):
        return True
    if tree["t"] in ("repr", "obj"):
        return False
    if tree["t"] == "dict":
        return all(_is_plain(k) and _is_plain(v) for k, v in tree["v"])
    return _is_plain(tree["v"])


def _encode(value: Any, budget: list[int], ancestors: set[int]) -> Any:
    kind = type(value)
    budget[0] -= len(value) if kind is str else 8
    if budget[0] < 0:
        raise _TooBig
    if kind is Shown:
        return value.tree
    if value is None or kind is bool or kind is str:
        return value
    # Home Assistant's websocket layer serialises with orjson, which has no
    # integers beyond 64 bits and turns nan/inf into null.
    if kind is int and -(2**63) <= value < 2**63:
        return value
    if kind is float and math.isfinite(value):
        return value
    if id(value) in ancestors:
        return {"t": "repr", "r": "..."}
    try:
        parts = _parts(value, kind)
    except Exception:  # noqa: BLE001 - a property or descriptor can raise anything
        parts = None
    if parts is None:
        return {"t": "repr", "r": _encode_repr(value, budget)}
    tag, name, items = parts
    ancestors.add(id(value))
    try:
        if tag == "dict":
            encoded: Any = [
                [_encode(k, budget, ancestors), _encode(v, budget, ancestors)]
                for k, v in items
            ]
        elif tag == "obj":
            encoded = [[k, _encode(v, budget, ancestors)] for k, v in items]
        else:
            encoded = [_encode(item, budget, ancestors) for item in items]
    finally:
        ancestors.discard(id(value))
    if tag == "list":
        return encoded
    if tag == "obj":
        return {"t": "obj", "n": name, "f": encoded}
    return {"t": tag, "v": encoded}


def _encode_repr(value: Any, budget: list[int]) -> str:
    try:
        text = repr(value)
    except Exception as err:  # noqa: BLE001 - a broken __repr__ can raise anything
        text = f"<repr-error {str(err)!r}>"
    budget[0] -= len(text)
    if budget[0] < 0:
        raise _TooBig
    return text


_LIST_ITERATOR: type = type(iter([]))
_BUILTIN_CONTAINERS: tuple[tuple[str, type], ...] = (
    ("list", list),
    ("tuple", tuple),
    ("set", set),
    ("frozenset", frozenset),
)


def _parts(value: Any, kind: type) -> tuple[str, str, Any] | None:
    """How a value breaks down into (node tag, class name, items), or None
    for one that's only meaningful as its repr(). A class with a __repr__ of
    its own always is: that's its author saying how it should read.
    """
    code = getattr(kind.__repr__, "__code__", None)
    made_by = code.co_filename if code is not None else ""
    if isinstance(value, tuple) and made_by == collections.__file__:
        # collections.namedtuple's generated __repr__, so typing.NamedTuple too.
        return (
            "obj",
            kind.__name__,
            list(zip(getattr(kind, "_fields", ()), value, strict=True)),
        )
    if kind is _LIST_ITERATOR:
        # What it has left to give, read from a copy so it isn't used up.
        return "iter", "", list(copy.copy(value))
    if isinstance(value, dict):
        return ("dict", "", value.items()) if kind.__repr__ is dict.__repr__ else None
    for tag, base in _BUILTIN_CONTAINERS:
        if isinstance(value, base):
            return (tag, "", value) if kind.__repr__ is base.__repr__ else None
    if dataclasses.is_dataclass(value) and made_by in (
        dataclasses.__file__,
        reprlib.__file__,
    ):
        return (
            "obj",
            kind.__name__,
            [
                (f.name, getattr(value, f.name))
                for f in dataclasses.fields(value)
                if f.repr
            ],
        )
    attributes = getattr(kind, "__attrs_attrs__", None)
    if attributes is not None and made_by.startswith("<attrs generated"):
        return (
            "obj",
            kind.__name__,
            [
                (
                    a.name,
                    _Shown(a.repr(getattr(value, a.name)))
                    if callable(a.repr)
                    else getattr(value, a.name),
                )
                for a in attributes
                if a.repr
            ],
        )
    return None


class _Shown:
    """A field that an attrs class formats with its own repr= callable."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


# The most show() sends back whatever it is asked for: enough for a screen
# or several, far short of what a registry or `hass.data` holds.
_SHOW_BUDGET = 50_000


class Shown:
    """What show() returns: a picture of an object rather than the object -
    its attributes and their values, already cut down to size. Echoed as
    the last expression, it is laid out and coloured by the client like any
    other value; `tree` is that picture, in _encode_value()'s terms."""

    def __init__(self, tree: Any) -> None:
        self.tree = tree

    def __repr__(self) -> str:
        return _tree_text(self.tree, "")


def show(
    thing: Any,
    *,
    private: bool = False,
    methods: bool = False,
    depth: int = 1,
    max_items: int = 30,
    max_string: int = 200,
) -> Shown:
    """Look inside an object: its attributes and their values, laid out
    one to a line. Names starting with an underscore are left out unless
    `private`, and methods unless `methods`, which lists them with their
    signatures. Properties are read, as that is where much of an object's
    state is.

    Long values are cut short: a container to its first `max_items`, a
    string or repr to `max_string` characters, each saying how much was
    left out. `depth` is how many levels are opened up - with 1, the
    object's own attributes and what is in any list or dict among them;
    with 2, the attributes of the objects it holds as well.
    """
    look = _Look(private, methods, depth, max_items, max_string)
    return Shown(look.at(thing, 0))


class _Look:
    def __init__(
        self, private: bool, methods: bool, depth: int, max_items: int, max_string: int
    ) -> None:
        self.private = private
        self.methods = methods
        self.depth = depth
        self.max_items = max(max_items, 1)
        self.max_string = max(max_string, 1)
        self.budget = _SHOW_BUDGET
        self.ancestors: set[int] = set()

    def at(self, value: Any, level: int) -> Any:
        """The tree for `value`, `level` levels below what show() was given."""
        kind = type(value)
        if value is None or kind is bool:
            return value
        if kind is int and -(2**63) <= value < 2**63:
            return value
        if kind is float and math.isfinite(value):
            return value
        if self.budget < 0:
            return _cut("...")
        if kind is str:
            self.budget -= min(len(value), self.max_string)
            if len(value) <= self.max_string:
                return value
            return _cut(f"{value[: self.max_string]!r}+{len(value) - self.max_string}")
        if kind is _Shown:
            return self._repr(value)
        if id(value) in self.ancestors:
            return _cut("...")
        try:
            parts = _parts(value, kind)
        except Exception:  # noqa: BLE001 - a property or descriptor can raise anything
            parts = None
        if parts is None:
            # What show() was given is always opened up, whatever the depth.
            below = level >= max(self.depth, 1)
            if below or isinstance(value, (bytes, bytearray)):
                return self._repr(value)
            parts = ("obj", kind.__qualname__, self._attributes(value))
        tag, name, items = parts
        items = list(items)
        if level > self.depth:
            if tag == "obj":
                return self._repr(value)
            # Not its repr(): building that for a big container costs as
            # much as sending it would.
            return _cut(f"<{kind.__name__} of {len(items)}>")
        # An object's own attributes are what was asked for, however many.
        more = 0 if tag == "obj" else len(items) - self.max_items
        if more > 0:
            items = items[: self.max_items]
        self.ancestors.add(id(value))
        try:
            if tag == "dict":
                encoded: Any = [
                    [self.at(k, level + 1), self.at(v, level + 1)] for k, v in items
                ]
                if more > 0:
                    encoded.append([_cut("..."), _cut(f"+{more}")])
            elif tag == "obj":
                encoded = [[k, self.at(v, level + 1)] for k, v in items]
            else:
                encoded = [self.at(item, level + 1) for item in items]
                if more > 0:
                    encoded.append(_cut(f"... +{more}"))
        finally:
            self.ancestors.discard(id(value))
        if tag == "list":
            return encoded
        if tag == "obj":
            return {"t": "obj", "n": name, "f": encoded}
        return {"t": tag, "v": encoded}

    def _repr(self, value: Any) -> Any:
        try:
            text = repr(value)
        except Exception as err:  # noqa: BLE001 - a broken __repr__ can raise anything
            text = f"<repr-error {str(err)!r}>"
        if len(text) > self.max_string:
            text = f"{text[: self.max_string]}...+{len(text) - self.max_string}"
        self.budget -= len(text)
        return _cut(text)

    def _attributes(self, value: Any) -> list[tuple[str, Any]]:
        try:
            names = dir(value)
        except Exception:  # noqa: BLE001 - a __dir__ of its own can raise anything
            names = []
        found: list[tuple[str, Any]] = []
        for name in names:
            if name.startswith("__") or (name.startswith("_") and not self.private):
                continue
            try:
                attr = getattr(value, name)
            except Exception as err:  # noqa: BLE001 - a property can raise anything
                attr = _Shown(f"<{type(err).__name__}: {err}>")
            if inspect.isroutine(attr) or inspect.isclass(attr):
                if not self.methods:
                    continue
                attr = _Shown(_callable_text(name, attr))
            found.append((name, attr))
        return found


def _cut(text: str) -> dict[str, str]:
    return {"t": "repr", "r": text}


def _callable_text(name: str, attr: Any) -> str:
    if inspect.isclass(attr):
        return f"class {attr.__qualname__}"
    try:
        signature = str(inspect.signature(attr))
    except TypeError, ValueError:
        signature = "(...)"
    prefix = "async def" if inspect.iscoroutinefunction(attr) else "def"
    return f"{prefix} {name}{signature}"


_BRACKETS = {
    "dict": ("{", "}"),
    "tuple": ("(", ")"),
    "set": ("{", "}"),
    "frozenset": ("frozenset({", "})"),
    "iter": ("<iterator [", "]>"),
}


def _tree_text(tree: Any, indent: str) -> str:
    """A show() tree as plain text, an attribute or item to a line - for a
    client that asks for no more than text, as `ha-repl --json` does."""
    inner = indent + "    "
    if isinstance(tree, list):
        opening, closing, lines = "[", "]", [_tree_text(i, inner) for i in tree]
    elif not isinstance(tree, dict):
        return repr(tree)
    elif tree["t"] == "repr":
        return tree["r"]
    elif tree["t"] == "obj":
        opening, closing = f"{tree['n']}(", ")"
        lines = [f"{k}={_tree_text(v, inner)}" for k, v in tree["f"]]
    elif tree["t"] == "dict":
        opening, closing = _BRACKETS["dict"]
        lines = [
            f"{_tree_text(k, inner)}: {_tree_text(v, inner)}" for k, v in tree["v"]
        ]
    else:
        opening, closing = _BRACKETS[tree["t"]]
        lines = [_tree_text(i, inner) for i in tree["v"]]
    if not lines:
        return opening + closing
    body = "".join(f"{inner}{line},\n" for line in lines)
    return f"{opening}\n{body}{indent}{closing}"


async def _maybe_await(value: Any) -> Any:
    """Finish a coroutine (or other awaitable) the user forgot to `await`;
    anything else passes straight through. Used both by the AST rewrite below
    (an unawaited call nested inside a larger expression) and _run_code's own
    check (a bare reference to one created earlier, e.g. `c = f(); c`)."""
    return await value if inspect.isawaitable(value) else value


def unawait(value: Any) -> Any:
    """Identity function and auto-await escape hatch: `unawait(f())` returns
    f()'s bare, un-awaited result (a coroutine, if f is async) - for when
    that's genuinely wanted, e.g. batching into `asyncio.gather(*[unawait(f())
    for f in ...])`. Recognised by name in the `_AutoAwait` rewrite below,
    which skips its whole argument rather than calling this at runtime; this
    plain version only runs if auto-await itself is off (`auto_await=False`)
    or `unawait` is used somewhere the rewrite doesn't reach.
    """
    return value


class _AutoAwait(ast.NodeTransformer):
    """Rewrites every call not already explicitly awaited to go through
    _maybe_await() first, so `hass.async_foo()` works whether or not the
    user remembered `await` - including nested inside attribute access
    (`hass.async_foo().attr`), a comprehension, or an argument list. Leaves
    nested (synchronous) function/lambda bodies alone: `await` there is a
    SyntaxError, and those calls run later, not as part of this statement
    anyway. A class body is left alone for the same first reason. `unawait(expr)` is the escape hatch - its argument is left
    completely untouched for when the bare coroutine is wanted. Opt out
    entirely with `auto_await=False` on the exec call (strict mode:
    forgetting await behaves exactly as in component code).
    """

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        return node

    def visit_Lambda(self, node: ast.Lambda) -> ast.Lambda:
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        # A class body runs synchronously, so nothing evaluated in it can be
        # awaited: not its own statements, nor its decorators and bases, nor
        # the decorators and defaults of its methods. Only what's inside an
        # async method's body can be.
        for stmt in node.body:
            if isinstance(stmt, ast.AsyncFunctionDef):
                stmt.body = [self.visit(inner) for inner in stmt.body]
            elif isinstance(stmt, ast.ClassDef):
                self.visit(stmt)
        return node

    def visit_Await(self, node: ast.Await) -> ast.Await:
        # Already explicit: don't double-await this call, but still rewrite
        # anything nested inside its own arguments.
        if isinstance(node.value, ast.Call):
            node.value.func = self.visit(node.value.func)
            node.value.args = [self.visit(a) for a in node.value.args]
            node.value.keywords = [self.visit(k) for k in node.value.keywords]
        else:
            node.value = self.visit(node.value)
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "unawait"
            and len(node.args) == 1
            and not node.keywords
        ):
            return node.args[0]
        self.generic_visit(node)
        wrapped = ast.Call(
            func=ast.Name(id="_maybe_await", ctx=ast.Load()), args=[node], keywords=[]
        )
        return ast.Await(value=wrapped)


async def _outcome(run: Any) -> tuple[Any, BaseException | None]:
    """What `run` returned, or what it raised. A task raising SystemExit or
    KeyboardInterrupt would take them out of the event loop rather than to
    whoever awaits it - out of Home Assistant itself."""
    try:
        return await run, None
    except asyncio.CancelledError:
        raise
    except BaseException as err:  # noqa: BLE001 - handed back, to be reported
        return None, err


async def _run_code(code: Any, globals_: dict[str, Any]) -> Any:
    # No separate "bare coroutine" backstop here: when auto_await is on, the
    # _AutoAwait rewrite already resolves every call site, and NOT doing so
    # unconditionally is exactly what `unawait(...)` asks for - a backstop
    # here would silently defeat it for a trailing `unawait(f())`.
    result = eval(code, globals_)  # nosec B307 - the whole point of a dev shell
    if code.co_flags & inspect.CO_COROUTINE:
        result = await result
    return result


def _capturing_print(out: io.StringIO):
    def shell_print(*args: Any, file: Any = None, **kwargs: Any) -> None:
        # stdout and stderr both come back to the client; other files are honoured.
        if file is None or file is sys.stdout or file is sys.stderr:
            file = out
        builtins.print(*args, file=file, **kwargs)

    return shell_print


def _capturing_help(out: io.StringIO, *, color: bool, width: int):
    # A fresh Helper per call, output redirected into the same buffer as print().
    helper = pydoc.Helper(input=io.StringIO(), output=out)

    def shell_help(*args: Any, full: bool = False) -> None:
        if not args:
            # help()'s real interactive loop (reading "help> " commands) spins the
            # CPU forever on this Python version when its input isn't a real tty -
            # verified in isolation, nothing project-specific about it - and there's no
            # live stdin to browse with anyway over this request/response API. Show
            # the same intro banner and stop there instead of entering interact().
            helper.intro()
            out.write(
                "\nGet help on any object, with links for known Home Assistant classes\n"
            )
            out.write("\ne.g. help(hass) or help(obj['/sun/sun']).\n")
            out.write(
                "\nhelp(thing, full=True) is Python's own full help page: every "
                "method's docstring, special methods, inherited members and data "
                "descriptors.\n"
            )
            return
        if len(args) > 1:
            helper(*args)  # raises the same TypeError real help() would
            return
        (thing,) = args
        if _is_summarisable(thing) and not full:
            out.write(_class_summary(thing, color=color, width=width))
        else:
            helper(thing)
        url = _doc_url(thing)
        if url:
            out.write(f"\nSee also: {url}\n")

    return shell_help


def _doc_url(obj: Any) -> str | None:
    cls = obj if inspect.isclass(obj) else type(obj)
    return _DOC_URLS.get(f"{cls.__module__}.{cls.__qualname__}")


def _is_summarisable(obj: Any) -> bool:
    """Classes and plain instances get the condensed summary below; modules,
    functions/methods, and primitives go through pydoc as usual (already short)."""
    return not (
        obj is None
        or isinstance(obj, (bool, int, float, complex, str, bytes))
        or inspect.ismodule(obj)
        or inspect.isroutine(obj)
    )


_SIGNATURE_LEXER = PythonLexer()
# The 16 named ANSI colours rather than a truecolor style: they follow the
# terminal's own palette, and travel through a websocket/terminal pair more
# predictably.
_SIGNATURE_FORMATTER = TerminalFormatter(bg="dark")


def _styled(text: str, codes: str, *, color: bool) -> str:
    return f"\x1b[{codes}m{text}\x1b[0m" if color and text else text


def _signature_line(line: str, *, color: bool, width: int) -> str:
    """A class/method signature, syntax-highlighted (pygments' Python lexer
    tokenizes it fine even though, as a bare "name(args) -> ret" fragment,
    it isn't valid standalone Python) - so type annotations, defaults and
    operators get real highlighting instead of being one flat-coloured
    string.

    Home Assistant's own methods lean hard on generics (Callable[[Unpack[
    _Ts]], ...]), so a real signature routinely runs past any reasonable
    width - wrapped here with a hanging indent *before* lexing (so the
    inserted newlines/spaces are just more whitespace tokens to pygments)
    rather than left to the terminal, which breaks at its own width with no
    indent at all and reads as a jumble of unrelated lines once there are
    dozens of methods back to back.
    """
    wrapped = "\n".join(
        textwrap.wrap(
            line,
            width=max(width, 20),
            subsequent_indent="      ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    )
    if not color:
        return wrapped
    tokens = list(lex(wrapped, _SIGNATURE_LEXER))
    while tokens and not tokens[-1][1].strip():
        tokens.pop()  # pygments always appends a trailing "\n" token
    return format_tokens(tokens, _SIGNATURE_FORMATTER)


def _class_summary(thing: Any, *, color: bool, width: int) -> str:
    """A class's full pydoc page repeats its docstring once per method, which
    balloons for a class like HomeAssistant with hundreds of methods. A method's
    own docstring is one `help(hass.the_method)` away, so here we only show the
    class docstring, the constructor signature, and the methods' names and
    signatures (no live attribute values either - that's what `hass.foo` is for).
    """
    cls = thing if inspect.isclass(thing) else type(thing)
    header = (
        f"Help on class {cls.__qualname__} in module {cls.__module__}:"
        if inspect.isclass(thing)
        else f"Help on {cls.__qualname__} object in module {cls.__module__}:"
    )
    parts = [_styled(header, "1", color=color), ""]
    doc = inspect.getdoc(cls)
    if doc:
        parts += [doc, ""]
    # A constructor "returning Self" is implied, not useful to state.
    ctor_sig = f"{cls.__qualname__}{_format_signature(cls, drop_return=(typing.Self,))}"
    parts.append(_signature_line(ctor_sig, color=color, width=width))
    method_names = sorted(
        name
        for name in dir(cls)
        if not name.startswith("_") and _is_plain_method(getattr(cls, name, None))
    )
    if method_names:
        parts += ["", _styled("Methods:", "1;4", color=color)]
        for name in method_names:
            sig = _format_signature(getattr(cls, name), drop_self=True)
            parts.append(_signature_line(f"  {name}{sig}", color=color, width=width))
    property_names = sorted(
        name
        for name in dir(cls)
        if not name.startswith("_") and isinstance(getattr(cls, name, None), property)
    )
    if property_names:
        parts += ["", _styled("Properties:", "1;4", color=color)]
        for name in property_names:
            doc = inspect.getdoc(getattr(cls, name))
            summary = doc.strip().splitlines()[0] if doc else ""
            line = f"  {name}" + (f" - {summary}" if summary else "")
            parts.append(
                textwrap.fill(line, width=max(width, 20), subsequent_indent="      ")
            )
    return "\n".join(parts)


def _is_plain_method(obj: Any) -> bool:
    # inspect.isroutine() also matches any non-data descriptor (defines __get__ but
    # not __set__) via its ismethoddescriptor() check - which, alongside genuine
    # methods, catches cached_property (both functools's and propcache's, used all
    # over Home Assistant's own entity classes), listing a read-only property as a
    # fake "name(...)" method. isfunction/ismethod is the properly narrow check.
    return inspect.isfunction(obj) or inspect.ismethod(obj)


# Matches a dotted path ending in an identifier, e.g. "homeassistant.core.State"
# or the "collections.abc.Coroutine" inside a bigger type expression - used to
# shorten type annotations down to just the class name.
_DOTTED_NAME = re.compile(r"\b(?:[A-Za-z_]\w*\.)+([A-Za-z_]\w*)\b")

# Python 3.14+ (PEP 649): __annotations__ access eagerly evaluates every annotation
# by default, which raises for names only imported under TYPE_CHECKING - common in
# HA's own codebase (e.g. Entity methods taking an "EntityPlatform"). FORWARDREF
# format resolves what it can and leaves the rest as an inert placeholder instead
# of raising. Not available before 3.14, hence the getattr dance.
_FORWARDREF_FORMAT = getattr(getattr(inspect, "Format", None), "FORWARDREF", None)


def _format_signature(
    target: Any, *, drop_self: bool = False, drop_return: tuple = ()
) -> str:
    """Render target's signature, trimmed down for a quick overview:
    self dropped (when `drop_self`), a void or otherwise uninformative return
    annotation dropped (`drop_return`), type annotations shortened to their bare
    class name, and no padding around `=` for default values.
    """
    sig = None
    if _FORWARDREF_FORMAT is not None:
        try:
            sig = inspect.signature(target, annotation_format=_FORWARDREF_FORMAT)
        except Exception:  # noqa: BLE001 - fall through to the attempts below
            sig = None
    if sig is None:
        try:
            sig = inspect.signature(target, eval_str=True)
        except Exception:  # noqa: BLE001 - unresolvable forward refs can raise almost
            # anything (NameError, AttributeError, ...); a signature with raw/partial
            # annotations is still better than losing the method entirely.
            try:
                sig = inspect.signature(target)
            except TypeError, ValueError:
                return "(...)"
    params = list(sig.parameters.values())
    if drop_self and params and params[0].name == "self":
        sig = sig.replace(parameters=params[1:])
    ret = sig.return_annotation
    if ret is not sig.empty and (
        ret is None or ret is type(None) or any(ret is d for d in drop_return)
    ):
        sig = sig.replace(return_annotation=sig.empty)
    # Annotations are plain strings in a module using `from __future__ import
    # annotations` - shown as written, not as quoted strings.
    text = _DOTTED_NAME.sub(r"\1", sig.format(quote_annotation_strings=False))
    return re.sub(r"\s*=\s*", "=", text)


def _format_error(err: BaseException) -> dict[str, Any]:
    """An error as data: its type and message, the standard traceback text,
    and - for the client to draw a fuller traceback from - each exception in
    its cause/context chain as "stacks", outermost (the one raised) first.
    """
    tb = err.__traceback__
    # Drop the frames belonging to this module so the traceback starts at user code.
    while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
        tb = tb.tb_next
    error: dict[str, Any] = {"type": type(err).__name__, "message": str(err)}
    if isinstance(err, SyntaxError):
        # No frames worth showing for this one, just the offending line and caret.
        error["traceback"] = "".join(traceback.format_exception_only(type(err), err))
        return error
    error["traceback"] = "".join(traceback.format_exception(type(err), err, tb))
    stacks = _stacks(err, tb)
    if stacks is not None:
        error["stacks"] = stacks
    return error


def _stacks(err: BaseException, tb: Any) -> list[dict[str, Any]] | None:
    """None if there's an exception group anywhere in the chain: the
    standard traceback text already lays those out as the tree they are."""
    stacks: list[dict[str, Any]] = []
    seen: set[int] = set()
    is_cause = False
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        if isinstance(current, BaseExceptionGroup):
            return None
        seen.add(id(current))
        frames = []
        while tb is not None:
            if tb.tb_frame.f_code.co_filename != __file__:
                frames.append(_frame(tb))
            tb = tb.tb_next
        hidden = max(len(frames) - _MAX_FRAMES, 0)
        if hidden:
            del frames[_MAX_FRAMES // 2 : _MAX_FRAMES // 2 + hidden]
        try:
            message = str(current)
        except Exception:  # noqa: BLE001 - a broken __str__ can raise anything
            message = "<exception str() failed>"
        stacks.append({
            "type": type(current).__name__,
            "message": message,
            "is_cause": is_cause,
            "notes": [str(n) for n in getattr(current, "__notes__", None) or ()],
            "frames": frames,
            "hidden": hidden,
        })
        if current.__cause__ is not None:
            current, is_cause = current.__cause__, True
        elif not current.__suppress_context__:
            current, is_cause = current.__context__, False
        else:
            break
        tb = current.__traceback__ if current is not None else None
    return stacks


def _frame(tb: Any) -> dict[str, Any]:
    code = tb.tb_frame.f_code
    lineno = tb.tb_lineno
    # Where in the line(s) it went wrong, for the client to mark. tb_lasti,
    # not the frame's own f_lasti: an outer frame has usually moved on (into
    # an except block, say) by the time anyone looks.
    start_line, end_line, start_col, end_col = next(
        itertools.islice(code.co_positions(), tb.tb_lasti // 2, None),
        (None, None, None, None),
    )
    position = (
        None
        if None in (start_line, end_line, start_col, end_col)
        else [start_line, start_col, end_line, end_col]
    )
    first = max(lineno - _CONTEXT_LINES, 1)
    last = max(lineno, min(end_line or lineno, lineno + 10)) + _CONTEXT_LINES
    return {
        "file": code.co_filename,
        "line": lineno,
        "name": code.co_name,
        "first": first,
        "source": linecache.getlines(code.co_filename)[first - 1 : last],
        "position": position,
    }


class SessionManager:
    """Holds sessions by name; a session lives until reset or process restart."""

    def __init__(
        self,
        bindings: dict[str, Any],
        features: frozenset[str] = frozenset(),
        *,
        complete_limit: int = 500,
        complete_budget: float = 0.5,
    ) -> None:
        self._bindings = bindings
        self._features = features
        # Most items, and seconds, ha_repl_server/complete spends on one answer.
        self.complete_limit = complete_limit
        self.complete_budget = complete_budget
        self._sessions: dict[str, Session] = {}

    def get(self, name: str) -> Session:
        if (session := self._sessions.get(name)) is None:
            globals_ = {
                "__name__": "__ha_repl__",
                "__builtins__": builtins,
                **self._bindings,
            }
            session = self._sessions[name] = Session(
                name, globals_, dict(self._bindings)
            )
        return session

    def namespace(self, name: str) -> dict[str, Any]:
        """A copy of what session `name` holds, or of the bindings alone if
        there is no such session - without creating it."""
        if (session := self._sessions.get(name)) is None:
            return {"__builtins__": builtins, **self._bindings}
        return {**session.globals_, **session.protected}

    def has_feature(self, name: str) -> bool:
        """Whether an optional capability that isn't a session binding is
        switched on - "sql" (the integration's expose_sql option) is served
        by its own websocket commands, not through exec."""
        return name in self._features

    def reset(self, name: str) -> bool:
        return self._sessions.pop(name, None) is not None

    def cancel(self, name: str | None, reason: str) -> list[str]:
        """Stop what's running in session `name`, or in every session if
        None, returning the names of those that had something to stop."""
        if name is None:
            sessions = list(self._sessions.values())
        else:
            sessions = [s] if (s := self._sessions.get(name)) else []
        return [s.name for s in sessions if s.cancel(reason)]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "created": s.created,
                "last_used": s.last_used,
                "executions": s.executions,
                "variables": sorted(
                    k
                    for k in s.globals_
                    if not k.startswith("__")
                    and k not in ("print", "help", "show", "_maybe_await", "unawait")
                ),
            }
            for s in self._sessions.values()
        ]
