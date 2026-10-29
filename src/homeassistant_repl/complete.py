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
import reprlib
import sys
import time
import types
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

# At most this many items in one answer, and this many seconds spent
# finding them; more of either makes it `incomplete`.
LIMIT = 500
BUDGET = 0.5

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


@dataclass
class Param:
    name: str
    text: str  # as written: `path: str = '/'`, `*args`
    kind: str  # inspect.Parameter's kind, by name


@dataclass
class Signature:
    """The signature of the call the cursor is in."""

    name: str
    params: list[Param]
    returns: str = ""
    doc: str = ""  # the docstring's first line

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": [vars(p) for p in self.params],
            "returns": self.returns,
            "doc": self.doc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signature:
        return cls(**{**data, "params": [Param(**p) for p in data["params"]]})

    def active(self, position: int, keyword: str | None) -> int | None:
        """Which parameter an argument at `position`, or given as
        `keyword`, goes to."""
        kinds = [p.kind for p in self.params]
        if keyword is not None:
            for i, param in enumerate(self.params):
                if param.name == keyword and param.kind != "POSITIONAL_ONLY":
                    return i
            return kinds.index("VAR_KEYWORD") if "VAR_KEYWORD" in kinds else None
        positional = [
            i
            for i, kind in enumerate(kinds)
            if kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
        ]
        if position < len(positional):
            return positional[position]
        return kinds.index("VAR_POSITIONAL") if "VAR_POSITIONAL" in kinds else None


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
    source: str,
    cursor: int,
    namespace: Mapping[str, Any],
    *,
    limit: int = LIMIT,
    budget: float | None = BUDGET,
) -> Completions | None:
    """The completions at `cursor` in `source`, against `namespace`. None
    when the expression before the cursor can't be followed without running
    something, for a caller with another way to try."""
    clock = _Budget(budget)
    found = _complete(source[:cursor], namespace, limit, clock)
    if found is not None:
        found.incomplete |= clock.ran_out
    return found


class _Budget:
    """Stops going through something big once the time's up."""

    def __init__(self, seconds: float | None) -> None:
        self.deadline = None if seconds is None else time.monotonic() + seconds
        self.ran_out = False

    def take[T](self, items: Iterable[T]) -> Iterator[T]:
        for n, item in enumerate(items):
            if (
                self.deadline is not None
                and n % 64 == 0
                and time.monotonic() > self.deadline
            ):
                self.ran_out = True
                return
            yield item


def _complete(
    text: str, namespace: Mapping[str, Any], limit: int, clock: _Budget
) -> Completions | None:
    if (quote := _open_string(text)) is not None:
        return _complete_string(text, quote, namespace, limit, clock)
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
        return _matching(_members(found), prefix, limit, clock)
    if _IMPORTING.search(before.rsplit("\n", 1)[-1]):
        return None
    names = _keywords(text, namespace) | _names(namespace)
    return _matching(names, prefix, limit, clock)


def call_at(text: str) -> tuple[int, int, str | None] | None:
    """The innermost call the end of `text` is inside: where its `(` is,
    which argument is being typed (counting from 0), and the keyword it's
    being given as, if any. None outside a call's brackets."""
    stack: list[list[Any]] = []  # bracket, where, commas, last comma
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif text.startswith(quote, i):
                i += len(quote) - 1
                quote = ""
        elif ch == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch in "'\"":
            quote = ch * 3 if text.startswith(ch * 3, i) else ch
            i += len(quote) - 1
        elif ch in "([{":
            stack.append([ch, i, 0, i])
        elif ch in ")]}" and stack:
            stack.pop()
        elif ch == "," and stack:
            stack[-1][2] += 1
            stack[-1][3] = i
        i += 1
    if not stack or stack[-1][0] != "(":
        return None
    _, paren, commas, last = stack[-1]
    named = re.match(r"\s*(\w+)\s*=(?!=)", text[last + 1 :])
    return paren, commas, named.group(1) if named else None


def signature(
    source: str, cursor: int, namespace: Mapping[str, Any]
) -> Signature | None:
    """The signature of the call `cursor` is inside, if it can be found
    without running anything."""
    text = source[:cursor]
    if (call := call_at(text)) is None:
        return None
    if (callee := _expression_before(text[: call[0]])) is None:
        return None
    found = _resolve(callee, namespace)
    if not isinstance(found, _Value):
        return None
    obj = found.obj
    func = obj
    if isinstance(obj, type):
        func = inspect.getattr_static(obj, "__init__", None)
        if func is None or func is object.__init__:
            return None
    if (inspected := _inspect_signature(func)) is None:
        return None
    # As called: the name a function has for itself is a descriptor away.
    name = ast.unparse(callee).rsplit(".", 1)[-1].split("(", 1)[0]
    doc = _doc(obj)
    returns = inspected.return_annotation
    return Signature(
        name,
        [_param(p) for p in inspected.parameters.values()],
        "" if returns is inspect.Signature.empty else str(returns),
        next((line.strip() for line in doc.splitlines() if line.strip()), "")[:120]
        if isinstance(doc, str)
        else "",
    )


def _doc(obj: Any) -> str | None:
    """A docstring, read only where reading it can't run anything."""
    if isinstance(obj, type):
        doc = vars(obj).get("__doc__")
    elif isinstance(
        obj, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)
    ):
        doc = obj.__doc__
    else:
        doc = inspect.getattr_static(obj, "__doc__", None)
    return doc if isinstance(doc, str) else None


def _param(param: inspect.Parameter) -> Param:
    text = param.name
    if param.kind is param.VAR_POSITIONAL:
        text = "*" + text
    elif param.kind is param.VAR_KEYWORD:
        text = "**" + text
    if param.annotation is not param.empty:
        text += f": {param.annotation}"
    if param.default is not param.empty:
        default = param.default
        # Only a value whose repr can't run anything is shown as itself.
        plain = default is None or type(default) in (str, int, float, bool, bytes)
        text += f" = {default!r}" if plain else f" = <{type(default).__name__}>"
    return Param(param.name, text, param.kind.name)


def _keywords(text: str, namespace: Mapping[str, Any]) -> dict[str, Item]:
    """Inside a call, its parameters not given yet, as `name=`."""
    if (call := call_at(text)) is None or call[2] is not None:
        return {}
    found = signature(text, len(text), namespace)
    if found is None:
        return {}
    given = set(re.findall(r"(\w+)\s*=(?!=)", text[call[0] + 1 :]))
    return {
        f"{param.name}=": Item(f"{param.name}=", "parameter", param.text)
        for param in found.params
        if param.kind in ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY")
        and param.name not in given
    }


def _complete_string(
    text: str, quote: int, namespace: Mapping[str, Any], limit: int, clock: _Budget
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
    items = [Item(k, "key") for k in clock.take(keys) if partial.lower() in k.lower()]
    items.sort(key=lambda i: rank(i.label, partial))
    result = Completions(items[:limit], len(partial))
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


def expression_at(text: str, after: str = "") -> str:
    """The name or attribute chain the cursor is on - `text` is before it,
    `after` after it - or, between a call's brackets, the callee. "" if
    neither."""
    word = re.match(r"\w*", after).group()  # type: ignore[union-attr]
    full = text + word
    if (found := _expression_start(full)) is not None and full[found:].strip():
        return full[found:]
    if (call := call_at(text)) is not None:
        callee = text[: call[0]]
        if (found := _expression_start(callee)) is not None:
            return callee[found:]
    return ""


def _expression_start(text: str) -> int | None:
    i = _scan_back(text)
    return i if i is not None and _parse(text[i:]) is not None else None


def _expression_before(text: str) -> ast.expr | None:
    """The name, attribute, call or subscript chain that `text` ends with."""
    i = _scan_back(text)
    return None if i is None else _parse(text[i:])


def _scan_back(text: str) -> int | None:
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
    return i if depth == 0 else None


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


def _signature(func: Any, *, full: bool = False) -> str:
    if (inspected := _inspect_signature(func)) is None:
        return ""
    text = inspected.format(quote_annotation_strings=False)
    return text if full or len(text) <= 80 else text[:77] + "..."


def _inspect_signature(func: Any) -> inspect.Signature | None:
    try:
        # As written: evaluating annotations could run code.
        found = inspect.signature(func, annotation_format=annotationlib.Format.STRING)
    except TypeError, ValueError:
        return None
    params = list(found.parameters.values())
    if params and params[0].name in ("self", "cls"):
        # Called on an instance or class, it's never given.
        found = found.replace(parameters=params[1:])
    return found


def matches(name: str, typed: str) -> bool:
    """`typed` anywhere in `name`, in any case - so `state` finds
    `async_set_state` - leaving out private names until a `_` is typed."""
    if name.startswith("_") and not typed.startswith("_"):
        return False
    return typed.lower() in name.lower()


def rank(name: str, typed: str) -> tuple[bool, bool, str]:
    """Sorting `matches()`: names starting with what's typed first."""
    return (
        not name.lower().startswith(typed.lower()),
        name.startswith("__"),
        name.lower(),
    )


def _matching(
    members: dict[str, Item], prefix: str, limit: int, clock: _Budget | None = None
) -> Completions:
    pairs = members.items() if clock is None else clock.take(members.items())
    items = [item for name, item in pairs if matches(name, prefix)]
    # A call's own parameters first.
    items.sort(key=lambda i: (i.kind != "parameter", *rank(i.label, prefix)))
    return Completions(items[:limit], len(prefix), len(items) > limit)


@dataclass
class Source:
    """Where something's source is - for `thing??`."""

    name: str  # qualified, `homeassistant.core.HomeAssistant`
    file: str  # "" for something with no Python source, built in
    line: int
    url: str  # on GitHub, at the version running; "" if not known

    def as_dict(self) -> dict[str, Any]:
        return vars(self).copy()


# Where a package's source is on GitHub: repository, the folder the package
# is in there, and how a version is tagged ("dev" is the default branch).
_REPOSITORIES = (
    ("homeassistant_repl", "rhizomatics/homeassistant-repl", "src/", "v{}", "main"),
    (
        "custom_components.ha_repl_server",
        "rhizomatics/homeassistant-repl",
        "",
        "v{}",
        "main",
    ),
    ("homeassistant", "home-assistant/core", "", "{}", "dev"),
)


def source(
    expression: str, namespace: Mapping[str, Any], versions: Mapping[str, str]
) -> Source | None:
    """Where the source of what `expression` names is, found without
    running anything - for an instance, its class's. `versions` gives the
    version of a package _REPOSITORIES knows that isn't in sys.modules to
    ask, by its name there. None if it can't be followed."""
    node = _parse(expression)
    found = None if node is None else _resolve(node, namespace)
    if isinstance(found, _Type):
        target: Any = found.cls
    elif isinstance(found, _Value):
        target = found.obj
    else:
        return None
    target = _defined(target)
    if isinstance(target, types.FunctionType):
        module = target.__module__ or ""
        name = f"{module}.{target.__qualname__}"
        file, line = target.__code__.co_filename, target.__code__.co_firstlineno
    elif isinstance(target, types.ModuleType):
        module = name = vars(target).get("__name__", "")
        file, line = vars(target).get("__file__") or "", 1
    elif isinstance(target, type):
        module, name = target.__module__, _qualified(target)
        line = vars(target).get("__firstlineno__", 1)
        file = (
            vars(sys.modules[module]).get("__file__") or ""
            if module in sys.modules
            else ""
        )
    else:
        return None
    return Source(name, file, line, _url(module, file, line, versions) if file else "")


def _defined(obj: Any) -> Any:
    """What holds the source of `obj`: the function behind a method,
    property or decorator, an instance's class."""
    if isinstance(obj, property) and obj.fget is not None:
        obj = obj.fget
    if isinstance(obj, (staticmethod, classmethod, types.MethodType)):
        obj = obj.__func__
    while isinstance(obj, types.FunctionType) and "__wrapped__" in vars(obj):
        obj = vars(obj)["__wrapped__"]
    if not isinstance(obj, (types.FunctionType, types.ModuleType, type)):
        obj = type(obj)
    return obj


def _url(module: str, file: str, line: int, versions: Mapping[str, str]) -> str:
    path = module.replace(".", "/")
    rel = next(
        (end for end in (f"{path}.py", f"{path}/__init__.py") if file.endswith(end)), ""
    )
    if not rel:
        return ""
    for package, repository, folder, tag, branch in _REPOSITORIES:
        if module == package or module.startswith(package + "."):
            version = versions.get(package) or _version(package)
            if not version:
                return ""
            ref = branch if "dev" in version else tag.format(version)
            return f"https://github.com/{repository}/blob/{ref}/{folder}{rel}#L{line}"
    if module.split(".", maxsplit=1)[0] in sys.stdlib_module_names:
        v = sys.version_info
        ref = f"v{v.major}.{v.minor}.{v.micro}" if v.releaselevel == "final" else "main"
        return f"https://github.com/python/cpython/blob/{ref}/Lib/{rel}#L{line}"
    return ""


def _version(package: str) -> str:
    """Home Assistant's version, if it's running here."""
    const = (
        sys.modules.get("homeassistant.const") if package == "homeassistant" else None
    )
    version = vars(const).get("__version__") if const is not None else None
    return version if isinstance(version, str) else ""


@dataclass
class Doc:
    """What something is, found without running anything - for Ctrl-Space
    and `ha-repl inspect`."""

    name: str  # as asked about
    kind: str  # "class", "function", "module", "property" or "instance"
    type: str  # qualified, `homeassistant.core.StateMachine`
    signature: str = ""
    doc: str = ""
    value: str = ""  # a plain value's repr, which can't run anything

    def as_dict(self) -> dict[str, Any]:
        return vars(self).copy()


_PLAIN = (str, int, float, bool, bytes, type(None))


def describe(expression: str, namespace: Mapping[str, Any]) -> Doc | None:
    """What `expression` names. None if it can't be followed."""
    node = _parse(expression)
    if isinstance(node, ast.Attribute) and (prop := _property(node, namespace)):
        returns = _annotations(prop.fget).get("return", "") if prop.fget else ""
        return Doc(expression, "property", returns, doc=_docstring(prop.fget))
    found = None if node is None else _resolve(node, namespace)
    if isinstance(found, _Type):
        return Doc(
            expression, "instance", _qualified(found.cls), doc=_docstring(found.cls)
        )
    if not isinstance(found, _Value):
        return None
    obj = found.obj
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    if isinstance(obj, type):
        init = inspect.getattr_static(obj, "__init__", None)
        signature = (
            "" if init in (None, object.__init__) else _signature(init, full=True)
        )
        return Doc(expression, "class", _qualified(obj), signature, _docstring(obj))
    if isinstance(
        obj, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)
    ):
        return Doc(
            expression,
            "function",
            _qualified(obj),
            _signature(obj, full=True),
            _docstring(obj),
        )
    if isinstance(obj, types.ModuleType):
        return Doc(
            expression, "module", vars(obj).get("__name__", ""), doc=_docstring(obj)
        )
    value = reprlib.Repr(maxstring=200).repr(obj) if type(obj) in _PLAIN else ""
    declared = ""
    if isinstance(node, ast.Attribute) and (owner := _owner(node, namespace)):
        # As the class declares it - `list[SqlTable]` says more than `list`.
        declared = _declared(owner).get(node.attr, "")
    return Doc(
        expression,
        "instance",
        declared or _qualified(type(obj)),
        doc=_docstring(type(obj)),
        value=value,
    )


def _owner(node: ast.Attribute, namespace: Mapping[str, Any]) -> type | None:
    """The class whose attribute `node` reads."""
    owner = _resolve(node.value, namespace)
    if isinstance(owner, _Value):
        return owner.obj if isinstance(owner.obj, type) else type(owner.obj)
    return owner.cls if isinstance(owner, _Type) else None


def _property(node: ast.Attribute, namespace: Mapping[str, Any]) -> property | None:
    """The property `node` reads, if it reads one - for its own docstring."""
    owner = _resolve(node.value, namespace)
    if isinstance(owner, _Value):
        obj = owner.obj
        cls = obj if isinstance(obj, type) else type(obj)
    elif isinstance(owner, _Type):
        cls = owner.cls
    else:
        return None
    raw = inspect.getattr_static(cls, node.attr, None)
    return raw if isinstance(raw, property) else None


def _qualified(obj: Any) -> str:
    """A class's or function's full name. Read off the object itself: a C
    type keeps these out of its __dict__, and on a class or function they
    run nothing of its own."""
    module, name = obj.__module__, obj.__qualname__
    return name if module in ("builtins", None) else f"{module}.{name}"


def _docstring(obj: Any) -> str:
    doc = vars(obj).get("__doc__") if isinstance(obj, types.ModuleType) else _doc(obj)
    return inspect.cleandoc(doc)[:4000] if isinstance(doc, str) else ""
