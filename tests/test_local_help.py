"""help() for what runs in the shell's own process - see local_help.py."""

from __future__ import annotations

import dataclasses

from homeassistant_repl.api_objtree import ApiObjTree
from homeassistant_repl.local_session import LocalSession
from homeassistant_repl.sql import SqlColumn, SqlTable, SqlTool


@dataclasses.dataclass
class Widget:
    """A widget."""

    size: int
    _secret: int = 0

    @property
    def area(self) -> int:
        """Size squared.

        Not shown: only the first paragraph is."""
        return self.size**2

    def grow(self, by: int = 1) -> None:
        """Not shown either - help(w.grow) is for that."""

    def _hidden(self) -> None:
        pass


async def _help(capsys, code: str, **bindings) -> str:
    assert await LocalSession({"Widget": Widget, **bindings}).run(code)
    return capsys.readouterr().out


async def test_help_on_an_instance_is_a_summary(capsys):
    out = await _help(capsys, "help(Widget(2))")

    assert "Help on Widget object" in out
    assert "A widget." in out
    assert "  grow(by: int = 1)\n" in out  # no self, no `-> None`, no quotes
    assert "  area - Size squared.\n" in out
    assert "  size: int\n" in out
    for unwanted in ("_secret", "_hidden", "__init__", "__eq__", "Not shown"):
        assert unwanted not in out
    assert "Method resolution order" not in out
    assert "Data descriptors" not in out
    assert "Create:" not in out  # how to make one is for help(Widget)
    assert "help(thing, full=True)" in out


async def test_help_on_a_class_shows_how_to_create_one(capsys):
    out = await _help(capsys, "help(Widget)")

    assert "Help on class Widget" in out
    assert "  Widget(size: int, _secret: int = 0)\n" in out


async def test_help_full_is_pythons_own_page(capsys):
    out = await _help(capsys, "help(Widget(2), full=True)")

    assert "__init__" in out
    assert "Data descriptors defined here" in out
    assert "Not shown either" in out  # every method's own docstring


async def test_help_leaves_functions_modules_and_topics_to_pydoc(capsys):
    assert "Return the number of items" in await _help(capsys, "help(len)")
    assert "Help on package json" in await _help(capsys, "import json\nhelp(json)")
    assert "for_stmt" in await _help(capsys, "help('for')")


async def test_help_with_no_arguments_does_not_start_pydocs_prompt(capsys):
    out = await _help(capsys, "help()")

    assert "help(thing, full=True)" in out
    assert "help>" not in out


async def test_help_itself_says_how_to_use_it(capsys):
    assert "help(thing)" in await _help(capsys, "help")
    assert "help(thing)" in await _help(capsys, "help(help)")
    assert "make_help" not in await _help(capsys, "help")


async def test_help_on_obj_says_how_to_use_it(capsys):
    obj = ApiObjTree.__new__(ApiObjTree)  # help() only looks at the class

    out = await _help(capsys, "help(obj)", obj=obj)

    assert 'obj["/mqtt/sensor"]' in out
    assert "  find(path: str = '/', *, platform: str | list[str] | None = None," in out
    assert "  keys() -> OrderedKeysView\n" in out
    assert "__getitem__" not in out
    assert "Method resolution order" not in out


async def test_help_on_sql_shows_how_to_call_it(capsys):
    table = SqlTable("states", (SqlColumn("state_id", "INTEGER"),))
    sql = SqlTool(None, tables=[table])

    out = await _help(capsys, "help(sql)", sql=sql)
    assert "  sql(query: str, *, max_rows: int | None = sql.max_rows, legacy:" in out
    assert "  table(name: str, *, legacy: bool = False) -> SqlTable\n" in out

    out = await _help(capsys, "help(sql.table('states'))", sql=sql)
    assert "  column_names - Just the names, in schema order" in out
    assert "  class_name: str\n" in out


class Gadget:
    kind = "lamp"

    def __init__(self) -> None:
        self.name = "x" * 500
        self.numbers = list(range(100))
        self._secret = 1

    @property
    def state(self) -> str:
        return "on"

    @property
    def broken(self) -> str:
        raise ValueError("no")

    def turn_on(self, brightness: int = 1) -> None: ...


async def _show(capsys, code: str) -> str:
    assert await LocalSession({"Gadget": Gadget}).run(code)
    return capsys.readouterr().out


async def test_show_lists_public_attributes_with_their_values(capsys):
    out = await _show(capsys, "show(Gadget(), max_items=3, max_string=10)")

    assert out.startswith("Gadget(\n")
    assert "    kind='lamp',\n" in out
    assert "    state='on'\n" in out
    assert "    broken=<ValueError: no>,\n" in out
    assert "    name='xxxxxxxxxx'+490,\n" in out
    assert "    numbers=[0, 1, 2, ... +97],\n" in out
    assert "_secret" not in out
    assert "turn_on" not in out


async def test_show_private_and_methods_add_those(capsys):
    out = await _show(capsys, "show(Gadget(), private=True, methods=True)")

    assert "    _secret=1,\n" in out
    assert "    turn_on=def turn_on(brightness: int = 1)\n" in out
    assert "__init__" not in out


async def test_show_of_plain_data_is_the_data_cut_down(capsys):
    out = await _show(capsys, "show({'a': list(range(50))}, max_items=2)")

    assert out == "{'a': [0, 1, ... +48]}\n"
