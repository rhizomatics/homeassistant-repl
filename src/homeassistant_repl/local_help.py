"""`help()` for code that runs here rather than inside Home Assistant - API
client mode's `obj`, live mode's `sql` and its results, anything of your own.

The same idea as the server's condensed help (custom_components/
ha_repl_server/session.py, `_class_summary`), written again here rather
than shared, like the rest of the two sides: what an object is for and what
you can call on it, not pydoc's full page - which for a class adds every
special method, inherited method, data descriptor and the method resolution
order.
`help(thing, full=True)` is that full page.

`show()` is here on the same terms: the server's own (session.py, `show`)
sends back a picture of an object that only exists inside Home Assistant;
this one is for an object that is already here, which rich can be handed
directly.
"""

from __future__ import annotations

import dataclasses
import inspect
import pydoc
import sys
import textwrap
from typing import Any

from rich.console import Console
from rich.pretty import Pretty
from rich.text import Text

from .render import _Struct

_INTRO = (
    "help(thing) summarises an object: what it is, and its methods, "
    "properties and attributes.\n"
    "help(thing, full=True) is Python's own full help page: every method's "
    "docstring, special methods, inherited members and data descriptors.\n\n"
    "e.g. help(obj), help(sql) or help(sql.table('states'))"
)


class ShellHelp:
    __doc__ = _INTRO

    def __init__(self, console: Console) -> None:
        self._console = console

    def __repr__(self) -> str:
        # Typing `help` without calling it says how to, as Python's own
        # help does, rather than showing a function's address.
        return _INTRO

    def __call__(self, *args: Any, full: bool = False) -> None:
        console = self._console
        if not args:
            # Python's own help() with no arguments starts an interactive
            # prompt, which has no place inside this one.
            console.print(_INTRO, markup=False, highlight=False)
            return
        if len(args) > 1:
            raise TypeError(f"help() takes one object, not {len(args)}")
        (thing,) = args
        if thing is self:
            # help(help), and so `help?`: what it's for, not a summary of
            # this class.
            console.print(_INTRO, markup=False, highlight=False)
            return
        if full or not _is_summarisable(thing):
            # An explicit output, so pydoc writes the page out rather than
            # opening a pager on top of the shell.
            pydoc.Helper(output=sys.stdout)(thing)
            return
        console.print(_summary(thing, console.width))


def make_help(console: Console) -> ShellHelp:
    """A `help` that prints to this console."""
    return ShellHelp(console)


# What rich lays out well as it stands; anything else is shown by its attributes.
_AS_THEY_ARE: tuple[type, ...] = (
    *(bool, int, float, complex, str, bytes, bytearray),
    *(dict, list, tuple, set, frozenset),
)


class Shown:
    """What show() returns: the object, with how it is to be laid out when
    echoed as the last expression."""

    def __init__(self, target: Any, **limits: Any) -> None:
        self._target = target
        self._limits = limits

    def __rich__(self) -> Pretty:
        return Pretty(self._target, **self._limits)

    def __repr__(self) -> str:
        return repr(self._target)


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
    string to `max_string` characters, each saying how much was left out,
    and anything more than `depth` levels below the object's own
    attributes to `...`.
    """
    target = thing
    if not (
        thing is None
        or isinstance(thing, _AS_THEY_ARE)
        or dataclasses.is_dataclass(thing)
    ):
        cls = type(type(thing).__qualname__, (_Struct,), {})
        target = cls(_attributes(thing, private=private, methods=methods))
    return Shown(
        target, max_length=max_items, max_string=max_string, max_depth=depth + 1
    )


def _attributes(thing: Any, *, private: bool, methods: bool) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    for name in dir(thing):
        if name.startswith("__") or (name.startswith("_") and not private):
            continue
        try:
            attr = getattr(thing, name)
        except Exception as err:  # noqa: BLE001 - a property can raise anything
            attr = _Text(f"<{type(err).__name__}: {err}>")
        if inspect.isroutine(attr) or inspect.isclass(attr):
            if not methods:
                continue
            attr = _Text(
                f"class {attr.__qualname__}"
                if inspect.isclass(attr)
                else f"def {name}{_signature(attr)}"
            )
        found.append((name, attr))
    return found


class _Text:
    """A value shown as this text, rather than as a quoted string."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _is_summarisable(obj: Any) -> bool:
    """Classes and instances get the summary; modules, functions, strings
    (a topic or keyword to pydoc) and other primitives go to pydoc, whose
    page for those is short already."""
    return not (
        obj is None
        or isinstance(obj, (bool, int, float, complex, str, bytes))
        or inspect.ismodule(obj)
        or inspect.isroutine(obj)
    )


def _summary(thing: Any, width: int) -> Text:
    is_class = inspect.isclass(thing)
    cls = thing if is_class else type(thing)
    out = Text()

    def section(title: str, lines: list[str]) -> None:
        if lines:
            out.append("\n")
            out.append(title + "\n", style="bold underline")
            out.append("\n".join(lines) + "\n")

    kind = f"class {cls.__qualname__}" if is_class else f"{cls.__qualname__} object"
    out.append(f"Help on {kind} in module {cls.__module__}:\n", style="bold")
    doc = inspect.getdoc(cls)
    # A dataclass with no docstring of its own gets its signature as one.
    if doc and not doc.startswith(f"{cls.__name__}("):
        out.append(f"\n{doc}\n")

    if is_class:
        section("Create:", [_wrap(f"  {cls.__qualname__}{_signature(cls)}", width)])
    else:
        call = getattr(cls, "__call__", None)  # noqa: B004 - the method itself is wanted
        if inspect.isfunction(call):
            sig = _signature(call, drop_self=True)
            section("Call it:", [_wrap(f"  {_name_of(thing)}{sig}", width)])

    members = {
        name: inspect.getattr_static(cls, name, None)
        for name in dir(cls)
        if not name.startswith("_")
    }
    section(
        "Methods:",
        [
            _wrap(f"  {name}{_signature(member, drop_self=True)}", width)
            for name, member in members.items()
            if inspect.isfunction(member)
        ],
    )
    section(
        "Properties:",
        [
            _described(name, inspect.getdoc(member), width)
            for name, member in members.items()
            if isinstance(member, property)
        ],
    )
    if dataclasses.is_dataclass(cls):
        section(
            "Attributes:",
            [
                _wrap(f"  {field.name}: {_type_name(field.type)}", width)
                for field in dataclasses.fields(cls)
                if not field.name.startswith("_")
            ],
        )
    out.append("\nhelp(thing, full=True) for Python's own full help page.")
    return out


def _name_of(thing: Any) -> str:
    """What an instance is bound as in the shell, if it's one of ours."""
    return {"SqlTool": "sql"}.get(type(thing).__name__, type(thing).__name__)


def _described(name: str, doc: str | None, width: int) -> str:
    first = " ".join((doc or "").split("\n\n")[0].split())
    return _wrap(f"  {name}" + (f" - {first}" if first else ""), width)


def _wrap(line: str, width: int) -> str:
    return textwrap.fill(
        line,
        width=max(width, 20),
        subsequent_indent="      ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def _type_name(annotation: Any) -> str:
    return (
        annotation
        if isinstance(annotation, str)
        else inspect.formatannotation(annotation)
    )


def _signature(target: Any, *, drop_self: bool = False) -> str:
    try:
        sig = inspect.signature(target)
    except TypeError, ValueError:
        return "(...)"
    params = list(sig.parameters.values())
    if drop_self and params and params[0].name == "self":
        sig = sig.replace(parameters=params[1:])
    if sig.return_annotation in (None, "None", type(None)):
        sig = sig.replace(return_annotation=sig.empty)
    # Annotations are plain strings in a module using `from __future__ import
    # annotations` - shown as written, not as quoted strings.
    return sig.format(quote_annotation_strings=False)
