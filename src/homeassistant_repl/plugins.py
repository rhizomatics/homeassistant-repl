"""Plugins: plain Python run at the start of a session, as though
typed at the prompt - one top-level statement at a time, so in live mode
each goes to whichever side it belongs on (see repl.py's LiveSession).

Unlike the prompt, nothing is echoed, and a statement that fails is only a
warning: the rest of the file and the remaining files still run, so a
session always starts. Both the warnings and anything a file prints go to
stderr, leaving stdout to what `ha-repl exec` was actually asked for.

A file can tell where it's running from `MODE` and `SERVER` (see
bind_context), so one file can serve the live shell, exec and API client mode alike.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from .config import display_path
from .local_session import LocalSession, format_error

# Runs one statement quietly, handing back its error (in the shape the
# server reports one) or None.
Runner = Callable[[str], Awaitable[dict[str, Any] | None]]


def bind_context(globals_: dict[str, Any], mode: str, server: str | None) -> None:
    """Bind `MODE` ("live", "exec" or "api") and `SERVER` (the configured server's
    name, None if connected by URL) in a session's local namespace.

    Ordinary local variables, not the shell's own like `sql`: being plain
    data they're copied over with any statement that runs inside Home
    Assistant, so `if MODE != "api": x = hass...` works on either side.
    """
    globals_["MODE"] = mode
    globals_["SERVER"] = server


def bind_rebind(
    globals_: dict[str, Any],
    files: Sequence[Path],
    run: Runner,
    remote: Iterable[str] = (),
) -> None:
    """Bind `rebind()` in a session's local namespace, to put back what the
    shell itself bound there - everything in it as this is called, so call
    it once those are in place and before any plugin has run. `remote` are
    the names that live inside Home Assistant and have nothing local to
    put back (`hass` and `obj` in live mode): a local variable in the way
    of one is removed. `files` and `run` are as run_plugins() takes them.
    """
    remote = tuple(remote)
    running = False

    async def rebind(*, plugins: bool = True) -> None:
        """Put back the shell's own names - `obj`, `hass`, `sql`,
        `hass_api`, `show`, `help` and the rest - where any has been
        assigned over, and run the plugins again unless `plugins` is
        False. Nothing else in the session is touched."""
        nonlocal running
        for name in remote:
            globals_.pop(name, None)
        globals_.update(originals)
        if not plugins or running:  # a plugin calling this doesn't start them all again
            return
        running = True
        try:
            await run_plugins(files, run)
        finally:
            running = False

    globals_["rebind"] = rebind
    originals = dict(globals_)


def split_statements(source: str) -> list[tuple[int, str]]:
    """A file's top-level statements, each as (first line, source text)."""
    lines = source.splitlines()
    spans: list[list[int]] = []
    for stmt in ast.parse(source).body:
        start = min([
            stmt.lineno,
            *(d.lineno for d in getattr(stmt, "decorator_list", [])),
        ])
        end = stmt.end_lineno or stmt.lineno
        if spans and start <= spans[-1][1]:
            # Sharing a line (`a = 1; b = 2`) - can't be cut apart by line.
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    return [(start, "\n".join(lines[start - 1 : end])) for start, end in spans]


async def run_plugins(files: Iterable[Path], run: Runner) -> list[Path]:
    """Run each file through `run`. Returns the files that were loaded."""
    loaded: list[Path] = []
    for path in files:
        try:
            # A handful of small files, once, before the prompt appears.
            statements = split_statements(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as err:
            line = getattr(err, "lineno", None)
            _warn(path, line, f"{type(err).__name__}: {getattr(err, 'msg', err)}")
            continue
        loaded.append(path)
        for line, text in statements:
            error = await run(text)
            if error:
                _warn(path, line, _summary(error))
    return loaded


def local_runner(session: LocalSession) -> Runner:
    """A Runner for a session that's entirely local (API client mode)."""

    async def run(source: str) -> dict[str, Any] | None:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                await session.evaluate(source, echo=False)
        except asyncio.CancelledError:
            raise
        except BaseException as err:  # noqa: BLE001 - report everything, incl. SystemExit
            return format_error(err)
        return None

    return run


def _summary(error: dict[str, Any]) -> str:
    if error.get("type"):
        return f"{error['type']}: {error.get('message', '')}"
    lines = str(error.get("traceback", "")).strip().splitlines()
    return lines[-1] if lines else "failed"


def _warn(path: Path, line: int | None, message: str) -> None:
    where = display_path(path) + (f":{line}" if line else "")
    print(f"ha-repl: plugin: {where}: {message}", file=sys.stderr)
