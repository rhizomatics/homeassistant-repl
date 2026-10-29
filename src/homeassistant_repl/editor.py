"""The interactive shells' line editor: one prompt_toolkit buffer per
statement, not per line.

A whole `for` loop is typed, edited and kept in history as one entry, so Up
brings back the complete block and any line of it can be changed before
running it again - the same as Python 3.13+'s own REPL. Enter runs the code
once it's a complete statement and the cursor is at the end, otherwise it
starts a new line, indented to match (one level deeper after a `:`). As in
the classic REPL, a block is finished by an empty line; Alt-Enter (Esc then
Enter) runs whatever is there straight away.

Ctrl-C while code runs stops it - see run_interruptibly().
"""

from __future__ import annotations

import ast
import asyncio
import codeop
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexers.python import PythonLexer
from rich.text import Text

from .complete import expression_at
from .completer import HaCompleter
from .local_session import console
from .shellhelp import doc_text

INDENT = "    "

# A statement starting with one of these ends its block, so the line after it
# starts a level shallower.
_DEDENT_AFTER = ("return", "pass", "break", "continue", "raise")

# The toolbar holds only the signature of the call being typed: plain, not
# the usual reversed bar.
_STYLE = Style.from_dict({
    "bottom-toolbar": "noreverse",
    "signature": "",
    "signature.active": "bold underline",
    "signature.doc": "italic",
    "signature.ha": "fg:ansicyan",
})

_compiler = codeop.CommandCompiler()
_compiler.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def python_prompt(
    history: Path, completer: Completer | None = None
) -> PromptSession[str]:
    """A prompt reading one complete statement (or block) per call - with
    the signature of the call being typed under it, given an HaCompleter."""
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history)),
        lexer=PygmentsLexer(PythonLexer),
        multiline=True,
        key_bindings=_key_bindings(completer),
        prompt_continuation="... ",
        completer=completer,
        complete_while_typing=completer is not None,
        style=_STYLE,
    )
    if isinstance(completer, HaCompleter):
        completer.signatures.attach(session)
    return session


def is_complete(source: str) -> bool:
    """True once `source` is ready to run: a complete statement, a block
    ended by an empty line, or something that will never compile (run anyway,
    so the session reports the error like any other)."""
    lines = source.split("\n")
    if len(lines) > 1 and not lines[-1].strip():
        return True
    try:
        # None means incomplete input: keep reading continuation lines.
        return _compiler(source, "<ha_repl>", "single") is not None
    except SyntaxError, OverflowError, ValueError:
        return True


def next_indent(line: str) -> str:
    """The indentation for the line following `line`."""
    indent = line[: len(line) - len(line.lstrip())]
    stripped = line.strip()
    if stripped.endswith(":") and not stripped.startswith("#"):
        return indent + INDENT
    if stripped.split(" ", 1)[0] in _DEDENT_AFTER:
        return indent[: -len(INDENT)]
    return indent


def _key_bindings(completer: Completer | None = None) -> KeyBindings:
    kb = KeyBindings()

    if isinstance(completer, HaCompleter):
        # Ctrl-Shift-Space, as VSCode's Trigger Parameter Hints - most
        # terminals send it as Ctrl-Space, so this takes both.
        @kb.add("c-space")
        def _describe(event: KeyPressEvent) -> None:
            document = event.current_buffer.document
            expression = expression_at(
                document.text_before_cursor, document.text_after_cursor
            )
            if expression:
                event.app.create_background_task(_show(completer, expression))

    @kb.add("enter")
    def _enter(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            # A completion picked from the menu is already in: just close it.
            buffer.complete_state = None
            return
        document = buffer.document
        if document.is_cursor_at_the_end and is_complete(document.text):
            _accept(event)
        else:
            buffer.insert_text("\n" + next_indent(document.current_line_before_cursor))

    @kb.add("escape", "enter")
    def _run_now(event: KeyPressEvent) -> None:
        _accept(event)

    return kb


async def _show(completer: HaCompleter, expression: str) -> None:
    """What `expression` is, printed above the prompt, which keeps what's
    typed."""
    found = await completer.describe(expression)
    text = (
        doc_text(found)
        if found
        else Text(f"Can't tell what `{expression}` is without running it")
    )
    await run_in_terminal(lambda: console.print(text))


def _accept(event: KeyPressEvent) -> None:
    """Run the buffer, minus the empty line that ended a block - so history
    keeps just the code."""
    buffer = event.current_buffer
    buffer.text = buffer.text.rstrip()
    buffer.validate_and_handle()


async def run_interruptibly(
    run: Awaitable[Any], interrupt: Callable[[], bool] | None = None
) -> None:
    """Await `run`, with Ctrl-C stopping it instead of the shell.

    The first Ctrl-C goes to `interrupt`, if given: True means it has asked
    for the code to stop some other way (code running in Home Assistant,
    told to stop there) and `run` is left to finish with that code's own
    report of where it stopped. Otherwise, or on the next Ctrl-C, `run` is
    cancelled. Code that never gives the event loop back - a blocking call
    in local code - can't be cancelled, so a Ctrl-C arriving while the last
    one is still waiting for the loop raises KeyboardInterrupt in it instead.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(run)
    pending = False
    interrupted = False

    def stop() -> None:
        nonlocal pending, interrupted
        pending = False
        if interrupt is not None and not interrupted and interrupt():
            interrupted = True
        else:
            task.cancel()

    def on_sigint(_signum: int, _frame: Any) -> None:
        nonlocal pending
        if pending:
            raise KeyboardInterrupt
        pending = True
        loop.call_soon_threadsafe(stop)

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        print("Stopped", file=sys.stderr)
    finally:
        signal.signal(signal.SIGINT, previous)
