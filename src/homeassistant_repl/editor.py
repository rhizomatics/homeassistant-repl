"""The interactive shells' line editor: one prompt_toolkit buffer per
statement, not per line.

A whole `for` loop is typed, edited and kept in history as one entry, so Up
brings back the complete block and any line of it can be changed before
running it again - the same as Python 3.13+'s own REPL. Enter runs the code
once it's a complete statement and the cursor is at the end, otherwise it
starts a new line, indented to match (one level deeper after a `:`). As in
the classic REPL, a block is finished by an empty line; Alt-Enter (Esc then
Enter) runs whatever is there straight away.
"""

from __future__ import annotations

import ast
import codeop
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.lexers import PygmentsLexer
from pygments.lexers.python import PythonLexer

INDENT = "    "

# A statement starting with one of these ends its block, so the line after it
# starts a level shallower.
_DEDENT_AFTER = ("return", "pass", "break", "continue", "raise")

_compiler = codeop.CommandCompiler()
_compiler.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def python_prompt(history: Path) -> PromptSession[str]:
    """A prompt reading one complete statement (or block) per call."""
    return PromptSession(
        history=FileHistory(str(history)),
        lexer=PygmentsLexer(PythonLexer),
        multiline=True,
        key_bindings=_key_bindings(),
        prompt_continuation="... ",
    )


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


def _key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _enter(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        document = buffer.document
        if document.is_cursor_at_the_end and is_complete(document.text):
            _accept(event)
        else:
            buffer.insert_text("\n" + next_indent(document.current_line_before_cursor))

    @kb.add("escape", "enter")
    def _run_now(event: KeyPressEvent) -> None:
        _accept(event)

    return kb


def _accept(event: KeyPressEvent) -> None:
    """Run the buffer, minus the empty line that ended a block - so history
    keeps just the code."""
    buffer = event.current_buffer
    buffer.text = buffer.text.rstrip()
    buffer.validate_and_handle()
