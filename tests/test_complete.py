"""Tests for complete.py, the completion that runs nothing - shared by the
shell and the server, so tested once, here, without Home Assistant."""

from __future__ import annotations

import collections
import functools
import json
from pathlib import Path
from typing import Any, Self

import pytest

from homeassistant_repl import complete as complete_module
from homeassistant_repl.complete import (
    Item,
    call_at,
    complete,
    context_start,
    describe,
    expression_at,
    signature,
    source,
    string_at,
)

ROOT = Path(__file__).parent.parent


def test_shell_and_server_copies_are_the_same():
    shell = ROOT / "src" / "homeassistant_repl" / "complete.py"
    server = ROOT / "custom_components" / "ha_repl_server" / "complete.py"
    assert shell.read_text() == server.read_text()


touched: list[str] = []


class State:
    entity_id: str

    @property
    def domain(self) -> str:
        touched.append("domain")
        return "x"


class Watched:
    """Records anything done to it that could run code."""

    count: int

    @property
    def prop(self) -> int:
        touched.append("property")
        return 1

    @property
    def state(self) -> State | None:
        touched.append("property")
        return None

    def __getattr__(self, name: str):
        touched.append(f"getattr {name}")
        return 1

    def __getitem__(self, key):
        touched.append("getitem")
        return self

    def __dir__(self):
        touched.append("dir")
        return ["zzz"]

    def method(self, a: int, b: str = "x") -> Self:
        touched.append("call")
        return self

    def get(self, entity_id: str) -> State | None:
        touched.append("call")
        return None


class Tree:
    """Stands in for the object trees."""

    _ha_repl_inspectable = True

    def __getitem__(self, key: str) -> State:
        return State()

    def _ipython_key_completions_(self) -> list[str]:
        return ["/mqtt", "/mqtt/sensor", "sun.sun"]

    def find(self, path: str) -> list[State]:
        return []


@pytest.fixture
def ns():
    touched.clear()
    yield {"w": Watched(), "d": {"sensor": State(), 1: "x"}, "tree": Tree(), "n": 7}
    assert touched == [], "completion ran something"


def labels(source: str, ns, cursor: int | None = None) -> list[str]:
    result = complete(source, len(source) if cursor is None else cursor, ns)
    assert result is not None
    return [item.label for item in result.items]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("w.", {"prop", "state", "method", "get", "count"}),
        ("w.prop.", {"bit_length", "real"}),
        ("w.state.", {"entity_id", "domain"}),
        ("w.method().", {"method", "prop"}),
        ("w.get('sun.sun').", {"entity_id", "domain"}),
        ("d['sensor'].", {"entity_id", "domain"}),
        ("tree['sun.sun'].", {"entity_id"}),
        ("n.", {"bit_length"}),
        ("'abc'.", {"upper"}),
    ],
)
def test_attributes_complete_without_running_anything(ns, source, expected):
    assert expected <= set(labels(source, ns))


def test_a_prefix_narrows_and_hides_private_names_until_asked_for(ns):
    assert labels("w.me", ns) == ["method"]
    assert "__class__" not in labels("w.", ns)
    assert "__class__" in labels("w._", ns)
    result = complete("w.me", 4, ns)
    assert result is not None and result.replace == 2


def test_items_say_what_they_are(ns):
    result = complete("w.", 2, ns)
    assert result is not None
    items = {item.label: item for item in result.items}
    assert items["method"].kind == "function"
    assert items["method"].detail == "(a: int, b: str = 'x') -> Self"
    assert (items["prop"].kind, items["prop"].detail) == ("property", "int")
    assert (items["count"].kind, items["count"].detail) == ("variable", "int")


def test_what_cannot_be_followed_safely_is_none(ns):
    # A subscript of an unknown type, a name that isn't there, a call of
    # something unannotated: for the caller to try another way.
    for text in ("w['a'].", "nothere.", "w.nothere.", "(lambda: 1)()."):
        assert complete(text, len(text), ns) is None


def test_keys_complete_in_a_subscript(ns):
    assert labels('tree["/m', ns) == ["/mqtt", "/mqtt/sensor"]
    assert labels("tree['", ns) == ["/mqtt", "/mqtt/sensor", "sun.sun"]
    assert labels('d["', ns) == ["sensor"]
    result = complete('tree["/mq', 9, ns)
    assert result is not None and result.replace == 3


def test_paths_complete_in_find(ns):
    assert labels('tree.find("', ns) == ["/mqtt", "/mqtt/sensor"]


def test_other_strings_complete_nothing(ns):
    assert labels('print("/u', ns) == []
    assert labels('w["', ns) == []  # not a safe subscript


def test_bare_names_include_the_namespace_builtins_and_keywords(ns):
    names = labels("", ns)
    assert {"w", "tree", "print", "while", "match"} <= set(names)
    names = labels("tr", ns)
    # Starting with it first, in any case; then anywhere in the name.
    assert set(names[:3]) == {"tree", "True", "try"}
    assert {"str", "setattr"} <= set(names[3:])


def test_imports_are_left_to_someone_else(ns):
    assert complete("import o", 8, ns) is None
    assert complete("from os import pa", 17, ns) is None


def test_context_start():
    assert context_start("w.me") == 2
    assert context_start('tree["/mq') == 6
    assert context_start("pri") == 0


def test_too_many_is_marked_incomplete(ns):
    result = complete_module.complete("", 0, ns, limit=5)
    assert result is not None
    assert len(result.items) == 5
    assert result.incomplete


def test_a_missing_key_completes_nothing_rather_than_guessing(ns):
    assert labels("d['nothere'].", ns) == []
    assert labels("d['nothere'].x.", ns) == []


def test_call_at_finds_the_innermost_open_call_and_argument():
    assert call_at("f(") == (1, 0, None)
    assert call_at("f(a, b") == (1, 1, None)
    assert call_at("f(a, key=") == (1, 1, "key")
    assert call_at("f(g(1), [2, 3], 'x,y', h(") == (24, 0, None)
    assert call_at("f(g(1), [2, 3], 'x,(y', ") == (1, 3, None)
    assert call_at("f(a) ") is None
    assert call_at("x[1, ") is None
    assert call_at("f(a == b") == (1, 0, None)


def test_signature_of_the_call_being_typed(ns):
    found = signature("w.method(1, ", 12, ns)
    assert found is not None
    assert found.name == "method"
    assert [p.text for p in found.params] == ["a: int", "b: str = 'x'"]
    assert found.returns == "Self"
    assert found.active(1, None) == 1
    assert found.active(0, "b") == 1
    assert found.active(5, None) is None
    assert signature("tree.find(", 10, ns).doc == ""  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]
    get = signature("w.get(", 6, ns)
    assert get is not None and get.returns == "State | None"


def test_signature_of_a_class_is_its_init(ns):
    class Point:
        """A point."""

        def __init__(self, x: int, y: int = 0) -> None:
            pass

    found = signature("Point(", 6, {"Point": Point})
    assert found is not None
    assert (found.name, found.doc) == ("Point", "A point.")
    assert [p.name for p in found.params] == ["x", "y"]


def test_no_signature_outside_a_call_or_for_what_cannot_be_followed(ns):
    assert signature("w.method", 8, ns) is None
    assert signature("(1, ", 4, ns) is None
    assert signature("nothere(", 8, ns) is None


def test_keyword_arguments_complete_inside_a_call_first(ns):
    assert labels("w.method(", ns)[:2] == ["a=", "b="]
    assert labels("w.method(1, b", ns)[0] == "b="
    assert "a=" not in labels("w.method(a=1, ", ns)
    assert "b=" not in labels("w.method(b=", ns)  # typing its value


def test_typed_text_matches_anywhere_in_a_name_starts_first(ns):
    assert labels("w.thod", ns) == ["method"]
    assert labels("w.ET", ns) == ["get", "method"]
    assert labels('tree["sensor', ns) == ["/mqtt/sensor"]
    # `get` starts with it, so comes before the names it's only inside.
    assert labels("w.ge", ns) == ["get"]
    names = {"async_set_state": 1, "get_state": 1, "state_of": 1, "states": 1}
    assert labels("state", names) == [
        "state_of",
        "states",
        "async_set_state",
        "get_state",
    ]


def test_source_of_a_class_function_or_instance(ns):
    found = source("Tree", {"Tree": Tree}, {})
    assert found is not None
    assert found.name == f"{__name__}.Tree"
    assert found.file == __file__
    assert found.line == Tree.__firstlineno__  # ty: ignore[missing-attribute]
    assert found.url == ""  # tests aren't a known repository
    method = source("w.method", ns, {})
    assert method is not None and method.name.endswith("Watched.method")
    assert source("w", ns, {}).name.endswith("Watched")  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]
    assert source("nothere", ns, {}) is None


def test_source_links_to_the_version_running():
    import asyncio

    import homeassistant_repl.complete as complete_module

    found = source("asyncio.sleep", {"asyncio": asyncio}, {})
    assert found is not None
    assert found.url.startswith("https://github.com/python/cpython/blob/v3.")
    assert found.url.endswith(f"/Lib/asyncio/tasks.py#L{found.line}")
    ours = source(
        "complete",
        {"complete": complete_module.complete},
        {"homeassistant_repl": "0.14.0"},
    )
    assert ours is not None
    assert ours.url == (
        "https://github.com/rhizomatics/homeassistant-repl/blob/v0.14.0/"
        f"src/homeassistant_repl/complete.py#L{ours.line}"
    )
    dev = source(
        "complete",
        {"complete": complete_module.complete},
        {"homeassistant_repl": "dev"},
    )
    assert dev is not None and "/blob/main/" in dev.url
    assert source("len", {}, {}).file == ""  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]


def test_expression_at_the_cursor_or_the_call_it_is_in():
    assert expression_at("hass.sta", "tes.get") == "hass.states"
    assert expression_at("x = hass.states.get") == "hass.states.get"
    assert expression_at("obj.find('/sun', ") == "obj.find"
    assert expression_at("obj.find('/sun', dom") == "dom"
    assert expression_at("1 + ") == ""


def test_describe_without_running_anything(ns):
    method = describe("w.method", ns)
    assert method is not None
    assert (method.kind, method.signature) == (
        "function",
        "(a: int, b: str = 'x') -> Self",
    )
    instance = describe("w", ns)
    assert instance is not None
    assert (instance.kind, instance.type) == ("instance", f"{__name__}.Watched")
    assert instance.doc == "Records anything done to it that could run code."
    assert instance.value == ""  # its repr could run anything
    # Known only from an annotation:
    # A property: by its own annotation, without reading it.
    state = describe("w.state", ns)
    assert state is not None and (state.kind, state.type) == (
        "property",
        "State | None",
    )
    assert describe("n", ns).value == "7"  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]
    assert describe("nothere", ns) is None


def test_describe_a_class_and_a_module():
    import json

    found = describe("json", {"json": json})
    assert found is not None
    assert found.kind == "module" and found.doc.startswith(
        "JSON (JavaScript Object Notation)"
    )
    cls = describe("json.JSONDecoder", {"json": json})
    assert cls is not None and cls.kind == "class"
    assert cls.signature.startswith("(*, object_hook=None")


def test_a_time_budget_cuts_a_long_answer_short(ns):
    keys = {f"key{i}": i for i in range(100_000)}
    found = complete('k["', 3, {"k": keys}, budget=0)
    assert found is not None
    assert found.incomplete
    assert len(found.items) < 100
    assert not complete('k["', 3, {"k": keys}, limit=200_000, budget=None).incomplete  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]


def test_describe_a_property_by_its_own_docstring_and_annotation(ns) -> None:
    class Owner:
        size: list[int]

        @property
        def total(self) -> int:
            """How many there are."""
            touched.append("property")
            return 0

    owner = Owner()
    owner.size = [1]
    found = describe("o.total", {"o": owner})
    assert found is not None
    assert (found.kind, found.type, found.doc) == (
        "property",
        "int",
        "How many there are.",
    )
    declared = describe("o.size", {"o": owner})
    assert declared is not None and declared.type == "list[int]"
    assert describe("len", {}).type == "len"  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]
    assert describe("x", {"x": []}).type == "list"  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]


def test_describe_an_attribute_by_what_its_class_says_about_it() -> None:
    class Tool:
        """A tool.

        `.items` the things it holds - `.item(n)` picks one.

        Nothing about anything else.
        """

        items: list[str]

    tool = Tool()
    tool.items = ["a", "b"]
    found = describe("t.items", {"t": tool})
    assert found is not None
    assert (found.type, found.value) == ("list[str]", "2 items")
    assert found.doc == "`.items` the things it holds - `.item(n)` picks one."
    assert found.owner.endswith("Tool")
    assert describe("t", {"t": tool}).owner == ""  # type: ignore[union-attr]  # ty: ignore[missing-union-attribute]


class Services:
    def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict | None = None,
        target: dict | None = None,
    ) -> None:
        touched.append("call")

    def turn_on(self, entity_ids: list[str]) -> None:
        touched.append("call")


asked: list[tuple[str, dict[str, str]]] = []


def offered(name: str, given) -> list[Item] | None:
    asked.append((name, dict(given)))
    if name in ("entity_id", "entity_ids"):
        return [Item("light.kitchen", "entity", "Kitchen"), Item("sun.sun", "entity")]
    if name == "service" and given.get("domain") == "light":
        return [Item("turn_on", "action"), Item("toggle", "action")]
    return None


def strung(source: str, ns) -> list[str] | None:
    asked.clear()
    result = complete(source, len(source), {**ns, "s": Services()}, strings=offered)
    assert result is not None
    return [item.label for item in result.items]


@pytest.mark.parametrize(
    ("source", "expected", "name"),
    [
        # by parameter, by position or keyword, and by dict key
        ('w.get("', ["light.kitchen", "sun.sun"], "entity_id"),
        ('w.get("kit', ["light.kitchen"], "entity_id"),
        ('anything(entity_id="', ["light.kitchen", "sun.sun"], "entity_id"),
        ('s.async_call("light", "turn_on", target={"entity_id": "', None, "entity_id"),
        ("{'entity_id': 'sun", ["sun.sun"], "entity_id"),
        # in a list given to the name
        ('anything(entity_id=["sun.sun", "', None, "entity_id"),
        ('s.turn_on(["', None, "entity_ids"),
        # what else is given by name goes along
        ('s.async_call("light", "t', ["toggle", "turn_on"], "service"),
        ('s.async_call(domain="light", service="', None, "service"),
    ],
)
def test_strings_given_to_a_name_complete_from_strings(ns, source, expected, name):
    found = strung(source, ns)
    assert found == expected or (expected is None and found)
    assert asked[0][0] == name


def test_what_else_is_given_by_name_is_passed_along(ns):
    strung('s.async_call("light", "', ns)
    assert asked == [("service", {"domain": "light"})]
    strung('s.async_call(domain="light", service="', ns)
    assert asked == [("service", {"domain": "light"})]


def test_strings_is_not_asked_about_keys_paths_or_strings_given_to_nothing(ns):
    assert strung('d["', ns) == ["sensor"]
    assert strung('tree.find("/m', ns) == ["/mqtt", "/mqtt/sensor"]
    assert strung('print("', ns) == []
    assert strung('a == "', ns) == []
    assert strung('nothing.known("', ns) == []
    assert all(name != "entity_id" for name, _ in asked)


def test_nothing_offered_completes_nothing(ns):
    assert strung('s.async_call("', ns) == []
    assert asked == [("domain", {})]


def test_string_at_finds_any_open_string():
    assert string_at('sql("sel') == 5
    assert string_at('sql("""select\nfrom st') == 7
    assert string_at("f('''a''') ") is None
    assert string_at('x = "a" + ') is None


# The paths the tests above don't take - each checked for what it gives,
# against both copies of complete.py (see test_complete_server.py).


def _done(text: str, namespace: dict, **kwargs) -> complete_module.Completions | None:
    return complete_module.complete(text, len(text), namespace, **kwargs)


def _labels(found: complete_module.Completions | None) -> set[str]:
    assert found is not None
    return {item.label for item in found.items}


class Settings:
    """Settings for something.

    `.retries` is how many times to try again."""

    retries: int = 3


class Node:
    """A node in a tree.

    `.me` is the node itself, for chaining."""

    __slots__ = ("empty", "value")
    parent: Node | None

    def __init__(self) -> None:
        self.value = 3

    @classmethod
    def make(cls) -> Self:
        return cls()

    @staticmethod
    def helper() -> int:
        return 1

    @property
    def me(self) -> Self:
        return self


class Inspectable:
    """One of the project's own kind, whose attributes only look things up."""

    _ha_repl_inspectable = True

    @property
    def boom(self) -> int:
        raise RuntimeError("not there")

    @property
    def fine(self) -> int:
        return 1


def settings() -> Settings:
    return Settings()


def maybe_settings() -> Settings | None:  # as `Optional[...]`, below
    return None


def quoted() -> Settings:  # as a string inside the string annotation, below
    return Settings()


def ordered() -> collections.OrderedDict:
    return collections.OrderedDict()


def listed() -> list[Settings]:
    return []


# Set here, where no tool rewrites them, rather than written in the signatures
maybe_settings.__annotations__["return"] = "Optional[Settings]"
quoted.__annotations__["return"] = "'Settings'"


def test_a_signature_round_trips_and_places_keyword_arguments():
    def f(a, /, b, *args, c=1, **kw):
        pass

    found = complete_module.signature("f(", 2, {"f": f})
    assert found is not None
    assert complete_module.Signature.from_dict(found.as_dict()) == found
    assert [p.text for p in found.params] == ["a", "b", "*args", "c = 1", "**kw"]
    assert found.active(0, "c") == 3
    # A positional-only name given as a keyword goes to **kw
    assert found.active(0, "a") == 4
    assert found.active(5, None) == 2

    def g(x):
        pass

    plain = complete_module.signature("g(", 2, {"g": g})
    assert plain is not None
    assert plain.active(0, "nope") is None
    assert plain.active(3, None) is None


def test_no_signature_where_there_is_none_to_read():
    class Plain:
        pass

    assert complete_module.signature("Plain(", 6, {"Plain": Plain}) is None
    assert complete_module.signature("five(", 5, {"five": 5}) is None


def test_a_callable_instance_has_its_class_signature_and_docstring():
    class Greeter:
        """Says hello.

        To anyone."""

        def __call__(self, name: str) -> str:
            return name

    found = complete_module.signature("g(", 2, {"g": Greeter()})
    assert found is not None
    assert [p.name for p in found.params] == ["name"]
    assert found.doc == "Says hello."


def test_kind_at_ignores_escapes_triple_quotes_and_comments():
    kind_at = complete_module.kind_at
    assert kind_at('x = "a\\"b') == "string"
    assert kind_at('x = """doc') == "name"
    assert kind_at("x  # it's") == "name"
    assert kind_at("hass.st") == "attribute"


def test_call_at_skips_escaped_quotes_and_comments():
    text = 'f(a, "x\\"(y", # (not a bracket\n    b'
    assert complete_module.call_at(text) == (1, 2, None)


def test_nothing_to_follow_is_none():
    assert _done("+ .", {}) is None
    assert _done("missing().", {}) is None
    assert _done("d[k].", {"d": {}}) is None  # not a literal key
    assert _done("s[0].", {"s": "abc"}) is None  # a str isn't only looked up
    assert _done("five().", {"five": 5}) is None  # nothing says what it gives
    assert complete_module.describe("a..b", {}) is None
    assert complete_module.expression_at("+ (") == ""


def test_keys_only_where_they_can_be_looked_up():
    class Finder:
        def find(self, path: str) -> None:
            pass

        def other(self, path: str) -> None:
            pass

    assert _labels(_done('missing["', {})) == set()
    assert _labels(_done('t.find("', {"t": Finder()})) == set()  # not inspectable
    assert _labels(_done('t.other("', {"t": Finder()})) == set()
    assert _labels(_done('i["', {"i": Inspectable()})) == set()  # no keys to offer


def test_what_a_string_is_given_to():
    asked: list[tuple[str, dict[str, str]]] = []

    def strings(name: str, given: dict[str, str]) -> list[complete_module.Item]:
        asked.append((name, dict(given)))
        return [complete_module.Item("thing", "entity")]

    def f(x, b):
        pass

    ns = {"f": f, "d": {}}
    assert _labels(_done('[entity_id="', ns, strings=strings)) == {"thing"}
    _done('d[1, "', ns, strings=strings)  # a subscript's key: given to nothing
    _done('{1, "', ns, strings=strings)  # a set: given to nothing
    _done('f(a b, "', ns, strings=strings)  # can't read what's given before it
    _done('f(1, 2, 3, "', ns, strings=strings)  # more than f takes
    _done('f(x="a", **more, b="', ns, strings=strings)
    assert asked == [("entity_id", {}), ("b", {}), ("b", {"x": "a"})]


def test_attributes_followed_through_slots_annotations_and_methods():
    node = Node()
    ns = {"n": node, "Node": Node, "settings": settings}
    assert "bit_length" in _labels(_done("n.value.", ns))  # a __slots__ value
    assert _done("n.empty.", ns) is None  # a __slots__ value never set
    assert "make" in _labels(_done("n.parent.", ns))  # by its annotation
    assert "helper" in _labels(_done("n.make().", ns))  # Self, from a classmethod
    assert "bit_length" in _labels(_done("Node.helper().", ns))  # a staticmethod
    # A class default, but the annotation says what it is on an instance
    assert "bit_length" in _labels(_done("settings().retries.", ns))


def test_return_annotations_followed_by_name_never_evaluated():
    ns = {
        "maybe": maybe_settings,
        "quoted": quoted,
        "ordered": ordered,
        "listed": listed,
    }
    assert "retries" in _labels(_done("maybe().", ns))
    assert "retries" in _labels(_done("quoted().", ns))
    assert "move_to_end" in _labels(_done("ordered().", ns))
    assert "append" in _labels(_done("listed().", ns))


def test_members_of_inspectable_and_odd_objects():
    assert {"boom", "fine"} <= _labels(_done("i.", {"i": Inspectable()}))
    assert _labels(_done("i.boom.", {"i": Inspectable()})) == set()  # it raised

    class Loose:
        pass

    loose = Loose()
    own: dict[Any, Any] = vars(loose)
    own[1] = "a key no name can reach"
    vars(loose)["named"] = 2
    assert "named" in _labels(_done("o.", {"o": loose}))
    found = _done('"abc".upp', {})
    assert found is not None
    upper = next(i for i in found.items if i.label == "upper")
    assert upper.kind == "function"


def test_source_of_modules_methods_properties_and_wrapped_functions():
    module = source("json", {"json": json}, {})
    assert module is not None
    assert (module.name, module.line) == ("json", 1)
    assert module.file.endswith("json/__init__.py")

    method = source("m", {"m": Node().make}, {})
    assert method is not None and method.name.endswith("Node.make")
    prop = source("p", {"p": vars(Node)["me"]}, {})
    assert prop is not None and prop.name.endswith("Node.me")

    @functools.wraps(settings)
    def wrapper() -> Settings:
        return settings()

    wrapped = source("w", {"w": wrapper}, {})
    assert wrapped is not None and wrapped.name.endswith(".settings")
    assert source("missing", {}, {}) is None
    # This project's own code, with no version given or installed to link to
    ours = source("complete", {"complete": complete_module.complete}, {})
    assert ours is not None and ours.url == ""


def test_describe_properties_instances_and_staticmethods():
    me = complete_module.describe("n.me", {"n": Node()})
    assert me is not None
    assert me.kind == "property"
    assert "for chaining" in me.doc  # from the class's docstring
    assert me.owner.endswith("Node")
    made = complete_module.describe("settings()", {"settings": settings})
    assert made is not None and (made.kind, made.type.rsplit(".", 1)[-1]) == (
        "instance",
        "Settings",
    )
    helper = complete_module.describe("h", {"h": vars(Node)["helper"]})
    assert helper is not None and helper.kind == "function"
    assert complete_module.describe("missing.x", {}) is None
    retries = complete_module.describe("s.retries", {"s": Settings()})
    assert retries is not None
    assert "how many times" in retries.doc
