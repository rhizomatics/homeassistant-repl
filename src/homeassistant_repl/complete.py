"""Completion that runs nothing: the attribute chain or key at the cursor is
followed with inspect.getattr_static and type annotations, never by
evaluating it - no property, __getattr__, __getitem__ or call of anything
but a plain dict, list or tuple, or one of this project's own classes marked
`_ha_repl_inspectable` (the object trees and registry entries, whose
lookups only look things up). See RFC 0002.

Kept identical in homeassistant_repl/complete.py (the shell's local
completion) and custom_components/ha_repl_server/complete.py (the server's
`ha_repl_server/complete`); tests/test_complete.py checks they match.
Standard library only, and no Home Assistant imports, for the same reason.
"""

from __future__ import annotations

import annotationlib
import ast
import builtins
import inspect
import keyword
import re
import sys
import types
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# At most this many items in one answer; more makes it `incomplete`.
LIMIT = 500

# The methods whose first argument is a path in the tree they're called on.
_PATH_METHODS = frozenset({"find", "find_paths", "find_names"})

_IDENTIFIER_END = re.compile(r"\w*$")
_IMPORTING = re.compile(r"(^|[\s;])(import|from)\s[\w.,\s]*$")
_MISSING = object()


@dataclass
class Item:
    label: str
    kind: str
    detail: str = ""


@dataclass
class Completions:
    """What can go at the cursor: each item's label replaces the `replace`
    characters before it. `incomplete` if more matched than were sent."""

    items: list[Item] = field(default_factory=list)
    replace: int = 0
    incomplete: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [
                {"label": i.label, "kind": i.kind, "detail": i.detail}
                for i in self.items
            ],
            "replace": self.replace,
            "incomplete": self.incomplete,
        }


@dataclass(frozen=True)
class _Value:
    """A real object, found without running anything - with the class it
    was found on, for a method's `Self`."""

    obj: Any
    owner: type | None = None


@dataclass(frozen=True)
class _Type:
    """Only the type is known, from an annotation."""

    cls: type


class _Absent:
    """Looked for in a way that can't run anything, and not there: nothing
    to complete, as opposed to not knowing."""


_ABSENT = _Absent()

_Found = _Value | _Type | _Absent


def context_start(text: str) -> int:
    """Where the word or string being typed at the end of `text` starts:
    what's before it decides what can complete it, and stays the same as
    more of it is typed."""
    if (string := _open_string(text)) is not None:
        return string + 1
    return _IDENTIFIER_END.search(text).start()  # type: ignore[union-attr]


def kind_at(text: str) -> str:
    """What's being typed at the end of `text`: "string", "attribute" (after
    a `.`) or "name"."""
    if _open_string(text) is not None:
        return "string"
    return "attribute" if text[: context_start(text)].endswith(".") else "name"


def complete(
    source: str, cursor: int, namespace: Mapping[str, Any], *, limit: int = LIMIT
) -> Completions | None:
    """The completions at `cursor` in `source`, against `namespace`. None
    when the expression before the cursor can't be followed without running
    something, for a caller with another way to try."""
    text = source[:cursor]
    if (quote := _open_string(text)) is not None:
        return _complete_string(text, quote, namespace, limit)
    prefix = _IDENTIFIER_END.search(text).group()  # type: ignore[union-attr]
    before = text[: len(text) - len(prefix)]
    if before.endswith("."):
        if (expr := _expression_before(before[:-1])) is None:
            return None
        found = _resolve(expr, namespace)
        if found is None:
            return None
        if isinstance(found, _Absent):
            return Completions()
        return _matching(_members(found), prefix, limit)
    if _IMPORTING.search(before.rsplit("\n", 1)[-1]):
        return None
    return _matching(_names(namespace), prefix, limit)


def _complete_string(
    text: str, quote: int, namespace: Mapping[str, Any], limit: int
) -> Completions:
    """Keys inside `x["`, and paths inside `obj.find("`. Nothing anywhere
    else in a string - not file names, say."""
    partial = text[quote + 1 :]
    before = text[:quote].rstrip()
    while before and before[-1] in "rRbBuU":
        before = before[:-1]
    keys: Iterable[str] = ()
    if before.endswith("["):
        if (expr := _expression_before(before[:-1])) is not None:
            target = _resolve(expr, namespace)
            if isinstance(target, _Value):
                keys = _keys(target.obj)
    elif before.endswith("("):
        call = _expression_before(before[:-1])
        if isinstance(call, ast.Attribute) and call.attr in _PATH_METHODS:
            target = _resolve(call.value, namespace)
            if isinstance(target, _Value) and _inspectable(target.obj):
                keys = (k for k in _keys(target.obj) if k.startswith("/"))
    items = [Item(k, "key") for k in keys if k.startswith(partial)]
    result = Completions(sorted(items, key=lambda i: i.label)[:limit], len(partial))
    result.incomplete = len(items) > limit
    return result


def _open_string(text: str) -> int | None:
    """Where the string literal open at the end of `text` starts, if one is."""
    line_start = text.rfind("\n") + 1
    quote: str | None = None
    start = 0
    i = line_start
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            if text.startswith(ch * 3, i):
                return None  # triple quoted: not a key
            quote, start = ch, i
        elif ch == "#":
            return None
        i += 1
    return start if quote is not None else None


def _expression_before(text: str) -> ast.expr | None:
    """The name, attribute, call or subscript chain that `text` ends with."""
    i = len(text)
    depth = 0
    while i > 0:
        ch = text[i - 1]
        if ch in ")]":
            depth += 1
        elif ch in "([":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and ch in "'\"" and (start := text.rfind(ch, 0, i - 1)) >= 0:
            i = start  # a string literal: `"abc".`
            continue
        elif depth == 0 and not (ch.isalnum() or ch in "_."):
            break
        i -= 1
    return _parse(text[i:]) if depth == 0 else None


def _parse(expr: str) -> ast.expr | None:
    if not expr.strip():
        return None
    try:
        return ast.parse(expr, mode="eval").body
    except SyntaxError, ValueError:
        return None


def _resolve(node: ast.expr, namespace: Mapping[str, Any]) -> _Found | None:
    match node:
        case ast.Name(id=name):
            if name in namespace:
                return _Value(namespace[name])
            if (value := getattr(builtins, name, _MISSING)) is not _MISSING:
                return _Value(value)
            return None
        case ast.Constant(value=value):
            return _Value(value)
        case ast.Attribute(value=owner, attr=name):
            found = _resolve(owner, namespace)
            if found is None or isinstance(found, _Absent):
                return found
            return _attribute(found, name)
        case ast.Subscript(value=owner, slice=key):
            found = _resolve(owner, namespace)
            if not isinstance(found, _Value):
                return found if isinstance(found, _Absent) else None
            try:
                index = ast.literal_eval(key)
            except ValueError, SyntaxError, TypeError:
                return None
            if not _subscriptable(found.obj):
                return None
            try:
                return _Value(found.obj[index])
            except Exception:  # noqa: BLE001 - a missing key is just nothing to complete
                return _ABSENT
        case ast.Call(func=func):
            found = _resolve(func, namespace)
            if not isinstance(found, _Value):
                return found if isinstance(found, _Absent) else None
            if isinstance(found.obj, type):
                return _Type(found.obj)
            return _returns(found.obj, found.owner)
    return None


def _attribute(found: _Value | _Type, name: str) -> _Found | None:
    if isinstance(found, _Value):
        obj = found.obj
        if _inspectable(obj):
            try:
                return _Value(getattr(obj, name))
            except Exception:  # noqa: BLE001 - not there is nothing to complete
                return _ABSENT
        raw = inspect.getattr_static(obj, name, _MISSING)
        owner = obj if isinstance(obj, type) else type(obj)
    else:
        owner = found.cls
        raw = inspect.getattr_static(owner, name, _MISSING)
    if raw is _MISSING:
        annotation = _declared(owner).get(name)
        return None if annotation is None else _annotated(annotation, owner)
    if isinstance(raw, property):
        return None if raw.fget is None else _returns(raw.fget, owner)
    if isinstance(raw, (staticmethod, classmethod)):
        return _Value(raw.__func__, owner)
    if isinstance(raw, types.FunctionType):
        return _Value(raw, owner)
    if isinstance(raw, types.MemberDescriptorType) and isinstance(found, _Value):
        try:
            return _Value(raw.__get__(found.obj))  # a __slots__ value: no code runs
        except AttributeError:
            return None
    if isinstance(found, _Type) and not callable(raw) and name in _declared(owner):
        # A class default, but the annotation says more about the instance's.
        return _annotated(_declared(owner)[name], owner)
    return _Value(raw)


def _subscriptable(obj: Any) -> bool:
    """Indexing it only looks something up."""
    if _inspectable(obj):
        return True
    for kind in (dict, list, tuple):
        if isinstance(obj, kind):
            return inspect.getattr_static(type(obj), "__getitem__") is kind.__getitem__
    return False


def _inspectable(obj: Any) -> bool:
    """One of this project's own classes, whose attributes, keys and dir()
    only look things up."""
    return inspect.getattr_static(type(obj), "_ha_repl_inspectable", False) is True


def _keys(obj: Any) -> Iterable[str]:
    if _inspectable(obj):
        method = inspect.getattr_static(type(obj), "_ipython_key_completions_", None)
        if method is not None:
            return method(obj)
    if isinstance(obj, dict) and _subscriptable(obj):
        return (k for k in dict.keys(obj) if isinstance(k, str))
    return ()


def _returns(func: Any, owner: type | None = None) -> _Type | None:
    """The type a call of `func` gives back, going by its annotation."""
    annotation = _annotations(func).get("return")
    if annotation is None:
        return None
    found = _annotated(annotation, owner, getattr(func, "__globals__", None))
    return found if isinstance(found, _Type) else None


def _annotations(obj: Any) -> dict[str, str]:
    """Annotations as written, as strings - without evaluating them."""
    try:
        return annotationlib.get_annotations(obj, format=annotationlib.Format.STRING)
    except Exception:  # noqa: BLE001 - no annotations to go by
        return {}


def _declared(cls: type) -> dict[str, str]:
    """The attribute annotations of a class and its bases."""
    declared: dict[str, str] = {}
    for klass in reversed(cls.__mro__):
        declared |= _annotations(klass)
    return declared


def _annotated(
    annotation: str, owner: type | None, globals_: Mapping[str, Any] | None = None
) -> _Type | None:
    """The class an annotation names - the first, of a union - looked up by
    name, never evaluated."""
    if globals_ is None:
        module = sys.modules.get(owner.__module__) if owner is not None else None
        globals_ = vars(module) if module is not None else {}
    node = _parse(annotation)
    for name in _named(node):
        if name in ("Self", "typing.Self") and owner is not None:
            return _Type(owner)
        cls = _lookup(name, globals_)
        if isinstance(cls, type) and cls is not type(None):
            return _Type(cls)
    return None


def _named(node: ast.expr | None) -> Iterable[str]:
    """The class names an annotation could mean, most likely first."""
    match node:
        case ast.Constant(value=str(text)):
            yield from _named(_parse(text))
        case ast.Name() | ast.Attribute():
            yield ast.unparse(node)
        case ast.BinOp(op=ast.BitOr(), left=left, right=right):
            yield from _named(left)
            yield from _named(right)
        case ast.Subscript(value=origin, slice=args):
            if ast.unparse(origin).rsplit(".", 1)[-1] in ("Optional", "Union"):
                for arg in args.elts if isinstance(args, ast.Tuple) else [args]:
                    yield from _named(arg)
            else:
                yield from _named(origin)


def _lookup(dotted: str, globals_: Mapping[str, Any]) -> Any:
    first, *rest = dotted.split(".")
    value = globals_.get(first, getattr(builtins, first, _MISSING))
    for part in rest:
        if value is _MISSING:
            break
        value = inspect.getattr_static(value, part, _MISSING)
    return None if value is _MISSING else value


def _members(found: _Value | _Type) -> dict[str, Item]:
    if isinstance(found, _Value) and _inspectable(found.obj):
        obj = found.obj
        return {n: _item(n, inspect.getattr_static(obj, n, None)) for n in dir(obj)}
    if isinstance(found, _Value):
        obj = found.obj
        classes = obj.__mro__ if isinstance(obj, type) else type(obj).__mro__
        try:
            # Past any __getattribute__ of the object's own class.
            own = object.__getattribute__(obj, "__dict__")  # noqa: PLC2801
        except AttributeError:
            own = {}
    else:
        classes, own = found.cls.__mro__, {}
    members: dict[str, Item] = {}
    for klass in reversed(classes):
        for name, annotation in _annotations(klass).items():
            members[name] = Item(name, "variable", annotation)
        for name, raw in vars(klass).items():
            members[name] = _item(name, raw)
    for name, raw in dict(own).items():
        if isinstance(name, str):
            members[name] = _item(name, raw)
    return members


def _names(namespace: Mapping[str, Any]) -> dict[str, Item]:
    names = {name: _item(name, value) for name, value in vars(builtins).items()}
    names |= {name: _item(name, value) for name, value in namespace.items()}
    names |= {kw: Item(kw, "keyword") for kw in [*keyword.kwlist, *keyword.softkwlist]}
    return names


def _item(name: str, raw: Any) -> Item:
    if isinstance(raw, property):
        returns = _annotations(raw.fget).get("return", "") if raw.fget else ""
        return Item(name, "property", returns)
    if isinstance(raw, (staticmethod, classmethod)):
        raw = raw.__func__
    if isinstance(raw, type):
        return Item(name, "class")
    if isinstance(raw, types.ModuleType):
        return Item(name, "module")
    if isinstance(
        raw, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)
    ):
        return Item(name, "function", _signature(raw))
    if callable(raw) and inspect.ismethoddescriptor(raw):
        return Item(name, "function")
    return Item(name, "variable", type(raw).__name__)


def _signature(func: Any) -> str:
    try:
        # As written: evaluating annotations could run code.
        signature = inspect.signature(
            func, annotation_format=annotationlib.Format.STRING
        )
        params = list(signature.parameters.values())
        if params and params[0].name in ("self", "cls"):
            # Completed on an instance or class, it's never typed.
            signature = signature.replace(parameters=params[1:])
        text = signature.format(quote_annotation_strings=False)
    except TypeError, ValueError:
        return ""
    return text if len(text) <= 80 else text[:77] + "..."


def _matching(members: dict[str, Item], prefix: str, limit: int) -> Completions:
    items = [
        item
        for name, item in members.items()
        if name.startswith(prefix)
        and (prefix.startswith("_") or not name.startswith("_"))
    ]
    items.sort(key=lambda i: (i.label.startswith("__"), i.label.lower()))
    return Completions(items[:limit], len(prefix), len(items) > limit)
