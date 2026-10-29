"""Tests for complete.py, the completion that runs nothing - shared by the
shell and the server, so tested once, here, without Home Assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Self

import pytest

from homeassistant_repl import complete as complete_module
from homeassistant_repl.complete import (
    call_at,
    complete,
    context_start,
    describe,
    expression_at,
    signature,
    source,
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
    assert signature("tree.find(", 10, ns).doc == ""  # type: ignore[union-attr]
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
    assert found.line == Tree.__firstlineno__
    assert found.url == ""  # tests aren't a known repository
    method = source("w.method", ns, {})
    assert method is not None and method.name.endswith("Watched.method")
    assert source("w", ns, {}).name.endswith("Watched")  # type: ignore[union-attr]
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
    assert source("len", {}, {}).file == ""  # type: ignore[union-attr]


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
    state = describe("w.state", ns)
    assert state is not None and state.type == f"{__name__}.State"
    assert describe("n", ns).value == "7"  # type: ignore[union-attr]
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
    assert not complete('k["', 3, {"k": keys}, limit=200_000, budget=None).incomplete  # type: ignore[union-attr]


def test_describe_a_property_by_its_own_docstring_and_annotation(ns):
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
    assert describe("len", {}).type == "len"  # type: ignore[union-attr]
    assert describe("[]", {}).type == "list"  # type: ignore[union-attr]
