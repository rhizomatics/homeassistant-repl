"""Tests for complete.py, the completion that runs nothing - shared by the
shell and the server, so tested once, here, without Home Assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Self

import pytest

from homeassistant_repl import complete as complete_module
from homeassistant_repl.complete import complete, context_start

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
    for source in ("w['a'].", "nothere.", "w.nothere.", "(lambda: 1)()."):
        assert complete(source, len(source), ns) is None


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
    assert labels("tr", ns) == ["tree", "try"]


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
