"""API client mode's exec engine: runs snippets right here in the CLI
process, not on a remote Home Assistant session - there's no server to send
code to, since API client mode talks to Home Assistant only through its
standard API (see api_objtree.py). Deliberately mirrors
custom_components/ha_repl_server/session.py's exec model (top-level await,
trailing-expression echo) rather than importing it:
that module lives in a separate Marketplace-deployed package with its own packaging
boundary, and duplicating ~100 lines here is simpler than bridging it.

Simpler than session.py in one respect: output goes straight to the real
terminal, so there's no print()/help() capturing-and-shipping-back machinery,
and rich's Console can auto-detect color/width instead of being told.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import inspect
import itertools
import linecache
import sys
import traceback
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.pretty import Pretty
from rich.traceback import Traceback

from .local_help import make_help, show

_cell_counter = itertools.count(1)

console = Console()
error_console = Console(stderr=True)


@dataclass
class LocalSession:
    """A namespace that persists between `run()` calls until the process exits."""

    globals_: dict[str, Any]
    auto_await: bool = True

    def __post_init__(self) -> None:
        self.globals_.setdefault("__name__", "__ha_repl_api__")
        self.globals_.setdefault("__builtins__", builtins)
        self.globals_.setdefault("_maybe_await", _maybe_await)
        self.globals_.setdefault("unawait", unawait)
        self.globals_.setdefault("help", make_help(console))
        self.globals_.setdefault("show", show)

    async def run(self, source: str, *, echo: bool = True) -> bool:
        """Execute source, printing the trailing expression's value (if any,
        and unless `echo` is off) or a traceback directly to the real
        terminal. Returns False if it raised."""
        try:
            value = await self.evaluate(source, echo=echo)
        except asyncio.CancelledError:
            # Cancellation of the caller (a timeout) must propagate, not be
            # reported as the snippet's own error.
            raise
        except BaseException as err:  # noqa: BLE001 - report everything, incl. SystemExit
            _print_error(err)
            return False
        if value is not None and echo:
            # A value that knows how to render itself (the Table show()
            # returns) is shown as-is rather than through Pretty's
            # repr-style output.
            rich_aware = not isinstance(value, type) and (
                hasattr(value, "__rich__") or hasattr(value, "__rich_console__")
            )
            console.print(value if rich_aware else Pretty(value))
        return True

    async def evaluate(self, source: str, *, echo: bool = True) -> Any:
        """Execute source and return the trailing expression's value (None
        if there isn't one), letting anything it raises propagate - for a
        caller that reports results its own way. `_` is set as run() would."""
        value = await self._execute(source)
        if value is not None and echo:
            self.globals_["_"] = value
        return value

    async def _execute(self, source: str) -> Any:
        # Not "<ha_repl-N>": rich.traceback refuses to show source for any
        # filename starting with "<" (treats it like "<stdin>"), no matter what
        # linecache holds. An absolute-looking path sidesteps that - rich joins a
        # relative one onto the cwd before the linecache lookup, which would miss.
        filename = f"/ha_repl/api_cell_{next(_cell_counter)}"
        linecache.cache[filename] = (
            len(source),
            None,
            # Always newline-terminated: rich's traceback rendering fails
            # ("substring not found") on a source with no newline in it at
            # all, i.e. any one-line command.
            (source if source.endswith("\n") else source + "\n").splitlines(
                keepends=True
            ),
            filename,
        )
        tree = ast.parse(source, filename, "exec")
        if self.auto_await:
            tree = _AutoAwait().visit(tree)
            ast.fix_missing_locations(tree)

        last_expr = None
        last_stmt = tree.body[-1] if tree.body else None
        if isinstance(last_stmt, ast.Expr):
            tree.body.pop()
            last_expr = ast.Expression(last_stmt.value)

        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        # dont_inherit=True: compile() otherwise inherits this module's own
        # `from __future__ import annotations`, which would make every
        # annotation in the user's code a plain string instead of a real value.
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
    plain version only runs if auto-await itself is off (--no-auto-await) or
    `unawait` is used somewhere the rewrite doesn't reach.
    """
    return value


class _AutoAwait(ast.NodeTransformer):
    """Rewrites every call not already explicitly awaited to go through
    _maybe_await() first, so `obj.async_method()` works whether or not the
    user remembered `await` - including nested inside attribute access
    (`obj.async_method().attr`), a comprehension, or an argument list.
    Leaves nested (synchronous) function/lambda bodies alone: `await` there
    is a SyntaxError, and those calls run later, not as part of this
    statement anyway. `unawait(expr)` is the escape hatch - its argument is
    left completely untouched for when the bare coroutine is wanted.
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


async def _run_code(code: Any, globals_: dict[str, Any]) -> Any:
    # No separate "bare coroutine" backstop here: when auto_await is on, the
    # _AutoAwait rewrite already resolves every call site, and NOT doing so
    # unconditionally is exactly what `unawait(...)` asks for - a backstop
    # here would silently defeat it for a trailing `unawait(f())`.
    result = eval(code, globals_)  # nosec B307 - the whole point of a dev shell
    if code.co_flags & inspect.CO_COROUTINE:
        result = await result
    return result


def _user_traceback(err: BaseException) -> Any:
    # Drop the shell's own leading frames so the traceback starts at user
    # code (compiled under /ha_repl/, see _execute) - whichever module of
    # ours called in. An error with no user frame at all is left whole.
    tb = err.__traceback__
    while tb is not None and not tb.tb_frame.f_code.co_filename.startswith("/ha_repl/"):
        tb = tb.tb_next
    return tb or err.__traceback__


def format_error(err: BaseException) -> dict[str, str]:
    """An error as plain data - the same shape the server reports one in."""
    return {
        "type": type(err).__name__,
        "message": str(err),
        "traceback": "".join(
            traceback.format_exception(type(err), err, _user_traceback(err))
        ),
    }


def _print_error(err: BaseException) -> None:
    tb = _user_traceback(err)
    if isinstance(err, SyntaxError):
        # No frames worth showing for this one - plain is fine.
        print(
            "".join(traceback.format_exception_only(type(err), err)),
            end="",
            file=sys.stderr,
        )
        return
    error_console.print(Traceback.from_exception(type(err), err, tb))
