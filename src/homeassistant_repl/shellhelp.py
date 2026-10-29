"""IPython style `?` and `??` in the shells - recognised by the input loop,
like quit(), and never run as code themselves.

- `?` on its own: what this shell offers, for the mode it's in
- `thing?` (or `?thing`): the shell's own help(thing), run where `thing`
  lives - so `hass?` is answered inside Home Assistant. It never calls
  anything: a call in `thing` is refused
- `thing??`: where its source is, with a link to that line on GitHub at
  the version running - found without running anything, see complete.py.
  A link rather than the source itself, since what's running in Home
  Assistant may be any version, and its files aren't here to show
"""

from __future__ import annotations

import ast
import re
from collections.abc import Awaitable, Callable
from typing import Any

from rich.text import Text

from .complete import Doc, Source
from .local_session import console

_REQUEST = re.compile(r"\s*(?P<before>\?\??)?(?P<expr>[^?\n]*?)\s*(?P<after>\?\??)?\s*")

_NAMES = {
    "live": [
        ("hass", "Home Assistant itself, inside Home Assistant"),
        ("obj", "the object tree: entities and actions by path - obj?"),
        ("sql", "the Recorder database, results downloaded here - sql?"),
        ("hass_api", "the REST API client, here"),
    ],
    "api": [
        ("obj", "the object tree: entities and actions by path - obj?"),
        ("hass_api", "the REST API client"),
    ],
}

_COMMON = [
    ("show(thing)", "what's inside an object, an attribute to a line"),
    ("help(thing)", "what an object is for, and what to call on it"),
    ("rebind()", "put back any of the names above, and run the plugins again"),
    ("MODE, SERVER", "the mode, and the server's name"),
]

_KEYS = [
    ("thing?", "help on thing, where it lives"),
    ("thing??", "where thing's source is, with a GitHub link at the version running"),
    (
        "Ctrl-Space",
        "what the name under the cursor is, or the call it's in, without running it",
    ),
    ("Tab", "complete; a menu also opens by itself after `.` and in keys"),
    ("Enter", "run, once the statement is complete; Alt-Enter runs it now"),
    ("Up", "bring back a whole earlier statement or block, to edit"),
    ("Ctrl-C", "stop what's running, or clear what's typed"),
    ("Ctrl-D", "leave"),
]


def help_request(source: str) -> tuple[str, str] | None:
    """("?", "thing") for `thing?` or `?thing`, ("??", "thing") for
    `thing??`, ("?", "") for `?` alone - None if `source` isn't one. A `?`
    is never Python outside a string, so nothing that is gets taken."""
    if "?" not in source or "\n" in source.strip():
        return None
    match = _REQUEST.fullmatch(source)
    if match is None:
        return None
    before, after = match["before"], match["after"]
    if bool(before) == bool(after):
        return None
    return before or after, match["expr"]


async def answer(
    request: tuple[str, str],
    mode: str,
    run: Callable[[str], Awaitable[Any]],
    locate: Callable[[str], Awaitable[Source | None]],
    describe: Callable[[str], Awaitable[Doc | None]],
) -> None:
    """Answer a help_request(): `run` runs code where the shell would,
    `locate` finds a source as `thing??` wants, `describe` what `thing` is
    without running it."""
    kind, expr = request
    if not expr:
        console.print(shell_help(mode))
        return
    try:
        node = ast.parse(expr, mode="eval")
    except SyntaxError:
        console.print(f"`{expr}` isn't a Python expression", style="red")
        return
    if any(isinstance(n, ast.Call) for n in ast.walk(node)):
        console.print(
            f"`{expr}{kind}` would call something to get there - "
            "ask about what's called instead, without the brackets",
            style="red",
        )
        return
    if kind == "?":
        found = await describe(expr)
        if found is not None and found.owner:
            # An attribute its class declares or documents: help() would
            # only describe the value's type - `list`, say.
            console.print(doc_text(found))
            return
        await run(f"help({expr})")
        return
    found = await locate(expr)
    if found is None:
        console.print(
            f"Can't tell where `{expr}` comes from without running it", style="red"
        )
    elif not found.file:
        console.print(f"{found.name} is built in: it has no Python source")
    else:
        # Unwrapped, so the link stays one link at any width.
        console.print(source_text(found), soft_wrap=True)


def shell_help(mode: str) -> Text:
    text = Text()
    title = "Live mode" if mode == "live" else "API mode"
    text.append(f"{title} - Python, with these to hand:\n\n", style="bold")
    rows = [*_NAMES.get(mode, []), *_COMMON]
    width = max(len(name) for name, _ in rows + _KEYS) + 2
    for name, what in rows:
        text.append(f"  {name:<{width}}", style="cyan")
        text.append(f"{what}\n")
    if mode == "live":
        text.append(
            "\nA statement using hass, obj, or a variable an earlier one left "
            "there, runs inside Home Assistant; anything else runs here.\n"
        )
    text.append("\n")
    for name, what in _KEYS:
        text.append(f"  {name:<{width}}", style="cyan")
        text.append(f"{what}\n")
    return text


def source_text(found: Source) -> Text:
    text = Text()
    text.append(found.name, style="bold")
    text.append(f"\n  {found.file}:{found.line}\n")
    if found.url:
        text.append("  ")
        text.append(found.url, style=f"link {found.url}")
        text.append("\n")
    return text


def doc_text(found: Doc) -> Text:
    """A description, as Ctrl-Space and `ha-repl inspect` show it."""
    text = Text()
    text.append(found.name, style="bold")
    if found.signature:
        text.append(found.signature, style="cyan")
    text.append(f"  {found.kind}", style="dim")
    if found.kind == "instance":
        text.append(f" of {found.type}", style="dim")
    elif found.kind == "property" and found.type:
        text.append(f" -> {found.type}", style="dim")
    if found.owner:
        text.append(f", attribute of {found.owner}", style="dim")
    if found.value:
        text.append(f"\n{found.value}")
    if found.doc:
        text.append(f"\n{found.doc}")
    return text
