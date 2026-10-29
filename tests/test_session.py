"""Tests for the execution engine, run without Home Assistant."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load session.py directly: importing the package would pull in Home Assistant.
_path = (
    Path(__file__).parent.parent / "custom_components" / "ha_repl_server" / "session.py"
)
_spec = importlib.util.spec_from_file_location("ha_repl_session", _path)
assert _spec is not None and _spec.loader is not None
session_mod = importlib.util.module_from_spec(_spec)
sys.modules["ha_repl_session"] = session_mod
_spec.loader.exec_module(session_mod)


@pytest.fixture
def manager():
    return session_mod.SessionManager({"hass": "fake-hass"})


async def run(manager, code, session="default", **kw):
    return await manager.get(session).run(code, **kw)


async def test_trailing_expression_is_echoed(manager):
    result = await run(manager, "x = 2\nx * 21")
    assert result.value == "42"
    assert result.error is None


async def test_statement_has_no_value(manager):
    assert (await run(manager, "y = 1")).value is None


async def test_bindings_available(manager):
    assert (await run(manager, "hass")).value == "'fake-hass'"


async def test_namespace_persists_and_resets(manager):
    await run(manager, "a = 5")
    assert (await run(manager, "a + 1")).value == "6"
    assert (await run(manager, "_")).value == "6"
    assert manager.reset("default") is True
    result = await run(manager, "a")
    assert result.error["type"] == "NameError"


async def test_sessions_are_isolated(manager):
    await run(manager, "a = 1", session="one")
    assert (await run(manager, "a", session="two")).error["type"] == "NameError"


async def test_print_and_stderr_captured(manager):
    result = await run(
        manager, "import sys\nprint('hi')\nprint('err', file=sys.stderr)"
    )
    assert result.stdout == "hi\nerr\n"


async def test_top_level_await(manager):
    code = "import asyncio\nawait asyncio.sleep(0)\nawait asyncio.sleep(0, result=7)"
    assert (await run(manager, code)).value == "7"


async def test_unawaited_coroutine_is_awaited_automatically(manager):
    # Forgetting `await` finishes the call instead of handing back an
    # unawaited coroutine object - the shell's job, not asyncio's.
    result = await run(manager, "import asyncio\nc = asyncio.sleep(0, result=9)\nc")
    assert result.value == "9"


async def test_unawaited_coroutine_in_attribute_chain_is_awaited(manager):
    code = (
        "class Thing:\n"
        "    domain = 'light'\n"
        "async def get_thing():\n"
        "    return Thing()\n"
        "get_thing().domain"
    )
    assert (await run(manager, code)).value == "'light'"


async def test_unawaited_coroutine_in_comprehension_is_awaited(manager):
    code = "async def double(n):\n    return n * 2\n[double(i) for i in range(3)]"
    assert (await run(manager, code)).value == "[0, 2, 4]"


async def test_auto_await_false_restores_strict_mode(manager):
    # Opt-out (ha-repl --no-auto-await): forgetting await behaves exactly as
    # it would in component code.
    result = await run(
        manager, "import asyncio\nc = asyncio.sleep(0, result=9)\nc", auto_await=False
    )
    assert result.value.startswith("<coroutine object sleep")
    await run(manager, "c.close()", auto_await=False)


async def test_auto_await_does_not_rewrite_nested_function_bodies(manager):
    # If the rewrite accidentally descended into this plain (sync) nested
    # def's body, wrapping the inner call in `await` would be a SyntaxError
    # ("await outside async function") - it must stay a plain, unawaited
    # call there, auto-awaited only once it surfaces from `wrapper()` itself.
    code = (
        "async def helper():\n"
        "    return 1\n"
        "def wrapper():\n"
        "    return helper()\n"
        "wrapper()"
    )
    result = await run(manager, code)
    assert result.error is None
    assert result.value == "1"


async def test_auto_await_does_not_rewrite_class_bodies(manager):
    # `await` in a class body is a SyntaxError, wherever in it the call is:
    # a field default, a decorator, a base class, a method's default.
    code = (
        "import asyncio, dataclasses\n"
        "def deco(arg):\n"
        "    return lambda cls: cls\n"
        "def base():\n"
        "    return object\n"
        "@deco(1)\n"
        "@dataclasses.dataclass\n"
        "class P(base()):\n"
        "    xs: list = dataclasses.field(default_factory=list)\n"
        "    class Inner:\n"
        "        n = len('abc')\n"
        "    async def twice(self, n=int('2')):\n"
        "        return asyncio.sleep(0, result=n * 2)\n"
        "[P().xs, P.Inner.n, P().twice()]"
    )
    result = await run(manager, code)
    assert result.error is None
    # The async method's own body is still rewritten, as is the call to it.
    assert result.value == "[[], 3, 4]"


async def test_unawait_returns_bare_coroutine(manager):
    # The escape hatch: the rewrite would otherwise auto-await this on its
    # own, which defeats the purpose of using unawait() here.
    result = await run(manager, "import asyncio\nc = unawait(asyncio.sleep(0))\nc")
    assert result.value.startswith("<coroutine object sleep")
    await run(manager, "c.close()")


async def test_unawait_enables_concurrent_gather(manager):
    # The motivating use case: batch unawaited coroutines for
    # asyncio.gather() instead of each one being awaited where it's created.
    code = (
        "import asyncio\n"
        "async def double(n):\n"
        "    return n * 2\n"
        "await asyncio.gather(*[unawait(double(i)) for i in range(3)])"
    )
    assert (await run(manager, code)).value == "[0, 2, 4]"


async def test_function_definitions_and_closures(manager):
    code = "async def f(n):\n    return [i * n for i in range(3)]\nawait f(2)"
    assert (await run(manager, code)).value == "[0, 2, 4]"


async def test_traceback_starts_at_user_code(manager):
    result = await run(manager, "def boom():\n    1 / 0\nboom()")
    tb = result.error["traceback"]
    assert result.error["type"] == "ZeroDivisionError"
    assert "1 / 0" in tb
    assert "session.py" not in tb


async def test_syntax_error(manager):
    result = await run(manager, "def (")
    assert result.error["type"] == "SyntaxError"


async def test_timeout(manager):
    result = await run(manager, "import asyncio\nawait asyncio.sleep(10)", timeout=0.05)
    assert result.error["type"] == "TimeoutError"


async def test_system_exit_is_reported_not_raised(manager):
    result = await run(manager, "raise SystemExit(3)")
    assert result.error["type"] == "SystemExit"


async def test_executions_serialised_within_session(manager):
    s = manager.get("default")
    await asyncio.gather(s.run("n = 0"), s.run("n += 1"), s.run("n += 1"))
    assert (await s.run("n")).value == "2"


async def test_describe(manager):
    await run(manager, "foo = 1")
    [info] = manager.describe()
    assert info["name"] == "default"
    assert "foo" in info["variables"]
    assert "print" not in info["variables"]
    assert "help" not in info["variables"]
    assert "show" not in info["variables"]
    assert "_maybe_await" not in info["variables"]
    assert "unawait" not in info["variables"]


async def test_help_on_module_is_captured_not_printed(manager, capsys):
    result = await run(manager, "import sys\nhelp(sys)")
    assert "sys" in result.stdout
    captured = capsys.readouterr()
    assert captured.out == ""  # must not leak to the real process stdout


async def test_help_on_keyword(manager):
    result = await run(manager, "help('for')")
    assert "for" in result.stdout.lower()


async def test_help_with_no_args_does_not_hang(manager):
    # pydoc's real interactive help> loop spins the CPU forever here when its
    # input isn't a real tty (confirmed against the stdlib directly, independent
    # of this codebase) - so bare help() shows the intro banner and stops there
    # rather than entering that loop.
    result = await run(manager, "help()")
    assert result.error is None
    assert "Welcome" in result.stdout
    assert "help(hass)" in result.stdout


async def test_doc_urls_loaded_from_yaml_file():
    # Guards the data file itself: it must parse, and a couple of entries we
    # know should be there (one hand-written, one from the per-domain sweep).
    assert (
        session_mod._DOC_URLS["homeassistant.core.HomeAssistant"]
        == "https://developers.home-assistant.io/docs/dev_101_hass"
    )
    assert (
        session_mod._DOC_URLS["homeassistant.components.light.LightEntity"]
        == "https://developers.home-assistant.io/docs/core/entity/light"
    )


async def test_help_shows_preloaded_doc_url_for_known_types(manager):
    # Fake out a HomeAssistant-typed object by monkeypatching the lookup table
    # directly would require importing the real class; instead exercise the
    # lookup helper itself.
    class FakeHass:
        pass

    FakeHass.__module__ = "homeassistant.core"
    FakeHass.__qualname__ = "HomeAssistant"
    assert (
        session_mod._doc_url(FakeHass())
        == "https://developers.home-assistant.io/docs/dev_101_hass"
    )


async def test_help_on_instance_is_condensed(manager):
    # Class docstring + constructor signature + method signatures only - no
    # per-method docstrings (that's what help(obj.the_method) is for) and no
    # live attribute dump.
    code = (
        "class Point:\n"
        "    '''A point.'''\n"
        "    def __init__(self, x, y):\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "    def dist(self, other):\n"
        "        '''Distance to another point.'''\n"
        "        return 0\n"
        "p = Point(1, 2)\n"
        "help(p)"
    )
    result = await run(manager, code)
    assert "A point." in result.stdout
    assert "Point(x, y)" in result.stdout
    assert "dist(other)" in result.stdout  # self dropped
    assert "Distance to another point." not in result.stdout
    assert "Current values:" not in result.stdout


async def test_help_excludes_cached_properties_from_methods(manager):
    # cached_property is a non-data descriptor (defines __get__ but not __set__),
    # which fools inspect.isroutine()'s method-descriptor check - it isn't a
    # method, it's a read-only property, and Home Assistant's own entity classes
    # lean on exactly this pattern (functools' and propcache's cached_property)
    # for things like `state`, `name`, `available`.
    code = (
        "import functools\n"
        "class Widget:\n"
        "    @functools.cached_property\n"
        "    def total(self):\n"
        "        return 42\n"
        "    def go(self):\n"
        "        pass\n"
        "w = Widget()\n"
        "help(w)"
    )
    result = await run(manager, code)
    assert "go()" in result.stdout
    assert "total" not in result.stdout


async def test_help_lists_properties_separately_from_methods(manager):
    code = (
        "class Widget:\n"
        "    '''A widget.'''\n"
        "    @property\n"
        "    def size(self):\n"
        "        '''How big it is.'''\n"
        "        return 42\n"
        "    def go(self):\n"
        "        pass\n"
        "w = Widget()\n"
        "help(w)"
    )
    result = await run(manager, code)
    assert "Properties:" in result.stdout
    assert "size - How big it is." in result.stdout
    assert "go()" in result.stdout


async def test_help_signature_formatting(manager):
    code = (
        "import decimal\n"
        "class Box:\n"
        "    def set(self, value: decimal.Decimal = decimal.Decimal(0), *, n: int = 1) -> None:\n"
        "        pass\n"
        "b = Box()\n"
        "help(b)"
    )
    result = await run(manager, code)
    # self dropped, no "-> None", "Decimal" not "decimal.Decimal", no spaces around "=".
    assert "set(value: Decimal=Decimal('0'), *, n: int=1)" in result.stdout
    assert "-> None" not in result.stdout
    assert "self" not in result.stdout


async def test_help_survives_unresolvable_forward_ref_annotation(manager):
    # Real-world trigger: homeassistant.helpers.entity.Entity has a method annotated
    # with "EntityPlatform", a name only imported under TYPE_CHECKING - so eval_str
    # resolution raises NameError (not TypeError/ValueError) deep inside inspect.
    # help() must fall back to the raw annotation rather than crash the session.
    code = (
        "class Widget:\n"
        "    def attach(self, platform: 'EntityPlatform') -> None:\n"
        "        pass\n"
        "w = Widget()\n"
        "help(w)"
    )
    result = await run(manager, code)
    assert result.error is None
    assert "attach(platform" in result.stdout
    assert "EntityPlatform" in result.stdout


async def test_help_constructor_drops_self_return(manager):
    # A class whose __new__ is annotated -> Self shouldn't show "-> Self" on the
    # constructor line (it's implied); session_mod._format_signature is exercised
    # directly since crafting a real __new__ -> Self case in exec'd source is awkward.
    class Widget:
        def __new__(cls) -> Widget:  # noqa: PYI034 - overwritten with Self below
            return super().__new__(cls)

    import typing

    Widget.__new__.__annotations__["return"] = typing.Self
    assert session_mod._format_signature(Widget, drop_return=(typing.Self,)) == "()"


async def test_help_full_is_pythons_own_page(manager):
    code = "class Point:\n    pass\np = Point()\n"
    assert "Data descriptors" not in (await run(manager, code + "help(p)")).stdout

    result = await run(manager, code + "help(p, full=True)")
    assert "Data descriptors defined here" in result.stdout


async def test_help_shows_string_annotations_unquoted(manager):
    code = (
        "class Box:\n"
        "    def put(self, thing: 'str | None' = None) -> 'Box':\n"
        "        return self\n"
        "help(Box())"
    )
    assert "put(thing: str | None=None) -> Box" in (await run(manager, code)).stdout


async def test_help_on_class_is_condensed(manager):
    code = (
        "class Point:\n"
        "    '''A point.'''\n"
        "    def __init__(self, x, y):\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "help(Point)"
    )
    result = await run(manager, code)
    assert "Help on class Point" in result.stdout
    assert "Point(x, y)" in result.stdout


async def test_help_on_method_keeps_its_docstring(manager):
    # Drilling into one specific method still gets the full pydoc treatment.
    code = (
        "class Point:\n"
        "    def dist(self, other):\n"
        "        '''Distance to another point.'''\n"
        "        return 0\n"
        "p = Point()\n"
        "help(p.dist)"
    )
    result = await run(manager, code)
    assert "Distance to another point." in result.stdout


async def test_value_is_also_sent_as_a_tree_for_the_client_to_render(manager):
    code = (
        "import collections, dataclasses\n"
        "hidden = dataclasses.field(default=0, repr=False)\n"
        "P = dataclasses.make_dataclass('P', [('x', int), ('hidden', int, hidden)])\n"
        "N = collections.namedtuple('N', 'a b')\n"
        "class Odd:\n"
        "    def __repr__(self):\n"
        "        return '<odd>'\n"
        "[1, 'a', None, True, 2.5, (1, 2), {'k': {3}}, frozenset(), P(1), N(1, 2), Odd()]"
    )
    assert (await run(manager, code)).value_tree == [
        1,
        "a",
        None,
        True,
        2.5,
        {"t": "tuple", "v": [1, 2]},
        {"t": "dict", "v": [["k", {"t": "set", "v": [3]}]]},
        {"t": "frozenset", "v": []},
        {"t": "obj", "n": "P", "f": [["x", 1]]},
        {"t": "obj", "n": "N", "f": [["a", 1], ["b", 2]]},
        {"t": "repr", "r": "<odd>"},
    ]


async def test_value_tree_keeps_to_what_json_can_carry(manager):
    # Home Assistant's websocket serialiser has no nan/inf or >64-bit ints.
    result = await run(manager, "[float('nan'), 2**70, b'x']")
    assert result.value_tree == [
        {"t": "repr", "r": "nan"},
        {"t": "repr", "r": "1180591620717411303424"},
        {"t": "repr", "r": "b'x'"},
    ]


async def test_value_tree_honours_a_custom_repr_on_a_container_or_dataclass(manager):
    code = (
        "import dataclasses\n"
        "class L(list):\n"
        "    def __repr__(self):\n"
        "        return 'L!'\n"
        "@dataclasses.dataclass\n"
        "class D:\n"
        "    x: int\n"
        "    def __repr__(self):\n"
        "        return 'D!'\n"
        "class Plain(dict):\n"
        "    pass\n"
        "[L([1]), D(1), Plain(a=1)]"
    )
    assert (await run(manager, code)).value_tree == [
        {"t": "repr", "r": "L!"},
        {"t": "repr", "r": "D!"},
        {"t": "dict", "v": [["a", 1]]},
    ]


async def test_value_tree_survives_cycles_and_broken_reprs(manager):
    assert (await run(manager, "l = []\nl.append(l)\nl")).value_tree == [
        {"t": "repr", "r": "..."}
    ]
    code = (
        "class Bad:\n    def __repr__(self):\n        raise RuntimeError('no')\nBad()"
    )
    result = await run(manager, code)
    assert result.error is None
    assert result.value_tree == {"t": "repr", "r": "<repr-error 'no'>"}
    assert result.value == "<repr-error 'no'>"


async def test_oversized_value_has_no_tree_just_truncated_text(manager):
    result = await run(manager, "['x' * 600_000, 'y' * 600_000]")
    assert result.value_tree is None
    assert result.truncated
    assert len(result.value) == session_mod.MAX_OUTPUT_CHARS


async def test_fetch_hands_back_only_plain_data(manager):
    code = (
        "plain = {'a': [1, (2, 3)], 4: {5}}\n"
        "names = iter(['x', 'y', 'z'])\n"
        "next(names)\n"
        "state = object()\n"
        "mixed = [1, object()]\n"
        "big = 'x' * 200_000\n"
        "lazy = (n for n in 'ab')"
    )
    wanted = ["plain", "names", "state", "mixed", "big", "lazy", "hass", "nope"]
    result = await run(manager, code, fetch=wanted)
    assert result.names == {
        "plain": {
            "t": "dict",
            "v": [
                ["a", [1, {"t": "tuple", "v": [2, 3]}]],
                [4, {"t": "set", "v": [5]}],
            ],
        },
        # What an iterator over a list has left - without using it up.
        "names": {"t": "iter", "v": ["y", "z"]},
    }
    assert (await run(manager, "list(names)")).value == "['y', 'z']"


async def test_error_frames_carry_their_source_and_position(manager):
    code = "def boom(n):\n    return 1 / n\n\nboom(0)"
    error = (await run(manager, code)).error
    [stack] = error["stacks"]
    assert (stack["type"], stack["message"]) == (
        "ZeroDivisionError",
        "division by zero",
    )
    outer, inner = stack["frames"]
    assert (outer["name"], outer["line"]) == ("<module>", 4)
    assert (inner["name"], inner["line"], inner["first"]) == ("boom", 2, 1)
    assert inner["file"] == outer["file"]
    assert inner["source"][inner["line"] - inner["first"]] == "    return 1 / n\n"
    assert inner["position"] == [2, 11, 2, 16]
    # Where the outer frame *was*, not where it has got to since.
    assert outer["position"] == [4, 0, 4, 7]


async def test_error_chain_is_sent_outermost_first(manager):
    code = (
        "try:\n"
        "    1 / 0\n"
        "except ZeroDivisionError as err:\n"
        "    err.add_note('careful')\n"
        "    raise ValueError('bad') from err"
    )
    raised, cause = (await run(manager, code)).error["stacks"]
    assert (raised["type"], raised["is_cause"]) == ("ValueError", False)
    assert (cause["type"], cause["is_cause"]) == ("ZeroDivisionError", True)
    assert cause["notes"] == ["careful"]


async def test_long_traceback_drops_its_middle_frames(manager):
    error = (await run(manager, "def r(n):\n    return r(n + 1)\nr(0)")).error
    [stack] = error["stacks"]
    assert len(stack["frames"]) == session_mod._MAX_FRAMES
    assert stack["hidden"] > 0
    assert stack["frames"][0]["name"] == "<module>"


async def test_syntax_error_and_exception_group_have_only_the_plain_traceback(manager):
    syntax = (await run(manager, "def (")).error
    assert "stacks" not in syntax
    assert "def (" in syntax["traceback"]
    group = (await run(manager, "raise ExceptionGroup('g', [ValueError('v')])")).error
    assert "stacks" not in group
    assert "ValueError: v" in group["traceback"]


async def test_help_summary_is_coloured_only_when_asked(manager):
    code = "class K:\n    def m(self, a: int = 1) -> str: ...\nhelp(K)"
    plain = (await run(manager, code)).stdout
    assert "\x1b[" not in plain
    assert "  m(a: int=1) -> str" in plain
    coloured = (await run(manager, code, color=True)).stdout
    assert "\x1b[1;4mMethods:\x1b[0m" in coloured


def test_server_side_code_does_not_import_rich():
    # Home Assistant bundles a rich of its own choosing; needing a newer one
    # here means upgrading it under a running process.
    for source in _path.parent.glob("*.py"):
        assert "rich" not in {
            line.split()[1].partition(".")[0]
            for line in source.read_text().splitlines()
            if line.lstrip().startswith(("import ", "from "))
        }, source.name
    assert not any(
        "rich" in r
        for r in json.loads((_path.parent / "manifest.json").read_text())[
            "requirements"
        ]
    )


_THING = """
import dataclasses

@dataclasses.dataclass
class Part:
    n: int = 1

class Thing:
    kind = "lamp"

    def __init__(self):
        self.name = "x" * 500
        self._secret = 1
        self.numbers = list(range(100))
        self.parts = {f"k{i}": Part(i) for i in range(40)}
        self.part = Part()
        self.other = Other()

    @property
    def state(self):
        return "on"

    @property
    def broken(self):
        raise ValueError("no")

    def turn_on(self, brightness: int = 1): ...

    async def go(self): ...

class Other:
    def __init__(self):
        self.depth = [[1, [2]]]

    def __repr__(self):
        return "<Other>"

thing = Thing()
"""


def _fields(tree):
    assert tree["t"] == "obj"
    return dict(tree["f"])


async def test_show_gives_public_attributes_with_their_values(manager):
    await run(manager, _THING)
    result = await run(manager, "show(thing)")

    assert result.error is None
    assert result.value_tree["n"] == "Thing"
    fields = _fields(result.value_tree)
    # not private names or methods, and properties are read
    assert list(fields) == [
        "broken",
        "kind",
        "name",
        "numbers",
        "other",
        "part",
        "parts",
        "state",
    ]
    assert fields["kind"] == "lamp"
    assert fields["state"] == "on"
    assert fields["broken"] == {"t": "repr", "r": "<ValueError: no>"}
    # an object below the top is its repr, a dataclass its fields
    assert fields["other"] == {"t": "repr", "r": "<Other>"}
    assert fields["part"] == {"t": "obj", "n": "Part", "f": [["n", 1]]}


async def test_show_cuts_long_values_short_and_says_by_how_much(manager):
    await run(manager, _THING)
    result = await run(manager, "show(thing, max_items=3, max_string=10)")

    fields = _fields(result.value_tree)
    assert fields["name"] == {"t": "repr", "r": "'xxxxxxxxxx'+490"}
    assert fields["numbers"] == [0, 1, 2, {"t": "repr", "r": "... +97"}]
    parts = fields["parts"]["v"]
    assert [k for k, _ in parts[:3]] == ["k0", "k1", "k2"]
    # below the depth asked for: its repr, not its fields
    assert parts[0][1] == {"t": "repr", "r": "Part(n=0)"}
    assert parts[3] == [{"t": "repr", "r": "..."}, {"t": "repr", "r": "+37"}]
    # every attribute is there, however few items are asked for
    assert len(fields) == 8


async def test_show_private_and_methods_add_those(manager):
    await run(manager, _THING)
    result = await run(manager, "show(thing, private=True, methods=True)")

    fields = _fields(result.value_tree)
    assert fields["_secret"] == 1
    assert fields["turn_on"] == {
        "t": "repr",
        "r": "def turn_on(brightness: int = 1)",
    }
    assert fields["go"] == {"t": "repr", "r": "async def go()"}
    assert not any(name.startswith("__") for name in fields)


async def test_show_depth_opens_up_the_objects_inside(manager):
    await run(manager, _THING)

    shallow = _fields((await run(manager, "show(thing, depth=0)")).value_tree)
    assert shallow["numbers"] == {"t": "repr", "r": "<list of 100>"}
    deep = _fields((await run(manager, "show(thing, depth=2)")).value_tree)
    assert _fields(deep["other"]) == {"depth": [{"t": "repr", "r": "<list of 2>"}]}


async def test_show_stops_at_its_budget_and_at_itself(manager):
    await run(
        manager,
        "big = [str(n) * 150 for n in range(2000)]\nloop = []\nloop.append(loop)",
    )

    result = await run(manager, "show(big, max_items=5000)")
    assert result.value_tree[-1] == {"t": "repr", "r": "..."}
    assert len(json.dumps(result.value_tree)) < 120_000
    assert (await run(manager, "show(loop)")).value_tree == [{"t": "repr", "r": "..."}]


async def test_show_as_text_is_an_attribute_to_a_line(manager):
    await run(manager, _THING)
    result = await run(manager, "show(thing.part)")

    assert result.value == "Part(\n    n=1,\n)"
    assert result.shown
    assert not (await run(manager, "thing.part")).shown
    result = await run(manager, "show({'a': [1, (2,)], 'b': set()}, depth=3)")
    assert result.value == (
        "{\n    'a': [\n        1,\n        (\n            2,\n        ),\n    ],\n"
        "    'b': {},\n}"
    )


async def test_cancel_stops_running_code_and_reports_where(manager):
    s = manager.get("default")
    run_ = asyncio.ensure_future(s.run("import asyncio\nawait asyncio.sleep(10)"))
    await asyncio.sleep(0.01)
    assert s.busy
    assert manager.cancel("default", "Stopped by tester") == ["default"]
    result = await run_
    assert result.error["type"] == "CancelledError"
    assert result.error["message"] == "Stopped by tester"
    assert "asyncio.sleep(10)" in result.error["traceback"]
    assert not s.busy
    # The session carries on as before.
    assert (await s.run("1 + 1")).value == "2"


async def test_cancel_with_no_session_named_stops_every_session(manager):
    runs = [
        asyncio.ensure_future(
            run(manager, "import asyncio\nawait asyncio.sleep(10)", name)
        )
        for name in ("a", "b")
    ]
    manager.get("idle")
    await asyncio.sleep(0.01)
    assert sorted(manager.cancel(None, "all")) == ["a", "b"]
    assert [(await r).error["type"] for r in runs] == ["CancelledError"] * 2


async def test_cancel_with_nothing_running_stops_nothing(manager):
    await run(manager, "x = 1")
    assert manager.cancel("default", "x") == []
    assert manager.cancel("missing", "x") == []


async def test_cancelling_the_caller_still_propagates(manager):
    run_ = asyncio.ensure_future(
        run(manager, "import asyncio\nawait asyncio.sleep(10)")
    )
    await asyncio.sleep(0.01)
    run_.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_
    assert not manager.get("default").busy


async def test_summary_says_how_a_run_went(manager):
    assert (await run(manager, "40 + 2")).summary().startswith("returned 42 in ")
    assert (await run(manager, "x = 1")).summary().startswith("finished in ")
    failed = (await run(manager, "1 / 0")).summary()
    assert failed.startswith("raised ZeroDivisionError: division by zero in ")
    printed = (await run(manager, "print('hi')")).summary()
    assert printed.endswith("s, 3 chars of output")
    long = (await run(manager, "'x' * 1000")).summary()
    assert len(long) < 300 and "\n" not in long
