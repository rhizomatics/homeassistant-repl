"""Tests for ObjTree's `mode("api"/"live")` switch, against a real (if
minimal) entity via pytest-homeassistant-custom-component - unlike
test_session.py's "fake-hass" string, this needs a genuine live Entity and
entity registry entry to exercise the api/live branches honestly."""

from __future__ import annotations

import re
from typing import Any

import pytest
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockEntity,
    setup_test_component_platform,
)

from custom_components.ha_repl_server.objtree import Action, ApiEntity, ObjTree


@pytest.fixture
async def obj(hass: HomeAssistant) -> ObjTree:
    one = MockEntity(entity_id="sensor.test_one", unique_id="t1", name="Test One")
    one._attr_state = "42"
    two = MockEntity(entity_id="sensor.test_two", unique_id="t2", name="Test Two")
    two._attr_state = "7"
    setup_test_component_platform(hass, "sensor", [one, two])
    assert await async_setup_component(
        hass, "sensor", {"sensor": [{"platform": "test"}]}
    )
    await hass.async_block_till_done()
    return ObjTree(hass)


async def test_default_mode_is_live(obj: ObjTree):
    assert obj.mode() == "live"
    entity = obj["sensor.test_one"]
    assert isinstance(entity, MockEntity)
    assert entity.state == "42"


async def test_mode_api_returns_dict_shaped_entity(obj: ObjTree):
    obj.mode("api")
    assert obj.mode() == "api"
    entity = obj["sensor.test_one"]
    assert isinstance(entity, ApiEntity)
    assert entity.entity_id == "sensor.test_one"
    assert entity.platform == "test"
    # ...and read as a live entity's would be
    assert entity.platform.platform_name == "test"
    assert entity.platform.domain == "sensor"
    assert entity.registry_entry.platform == "test"
    assert entity.unique_id == entity.registry_entry.unique_id
    assert entity.available is True
    assert entity.enabled is True
    assert entity.domain == "sensor"
    assert entity.object_id == "test_one"
    assert entity.state == "42"
    assert entity.name == "Test One"


async def test_mode_persists_on_derived_subtree(obj: ObjTree):
    obj.mode("api")
    subtree = obj["/test"]
    assert isinstance(subtree, ObjTree)
    entity = subtree["sensor/test_one"]
    assert isinstance(entity, ApiEntity)


async def test_mode_live_reverts(obj: ObjTree):
    obj.mode("api")
    obj.mode("live")
    assert obj.mode() == "live"
    assert isinstance(obj["sensor.test_one"], MockEntity)


async def test_mode_rejects_bad_value(obj: ObjTree):
    with pytest.raises(ValueError, match="mode must be"):
        obj.mode("bogus")


async def test_bare_entity_id_courtesy_lookup_at_root(obj: ObjTree):
    entity = obj["sensor.test_one"]
    assert isinstance(entity, MockEntity)


async def test_bare_entity_id_courtesy_lookup_scoped_to_integration(obj: ObjTree):
    scoped = obj["/test"]
    assert isinstance(scoped, ObjTree)
    entity = scoped["sensor.test_one"]
    assert isinstance(entity, MockEntity)
    assert entity is obj["sensor.test_one"]


async def test_bare_entity_id_courtesy_lookup_wrong_integration_fails(obj: ObjTree):
    # Constructed directly (not via obj["/not_test"]) so the mismatch itself
    # is what's under test, not an empty-subtree KeyError from navigating
    # into an integration scope with no entities at all.
    scoped = ObjTree(obj.hass, integration="not_test")
    with pytest.raises(KeyError):
        scoped["sensor.test_one"]


async def test_find_respects_mode(obj: ObjTree):
    obj.mode("api")
    found = list(obj.find())
    assert found and all(isinstance(f, ApiEntity) for f in found)
    obj.mode("live")
    found = list(obj.find())
    assert found and all(isinstance(f, MockEntity) for f in found)


async def test_find_paths_accepts_a_regex_prefix(obj: ObjTree):
    assert sorted(obj.find_paths("/test/sensor/test_one.*")) == [
        "/test/sensor/test_one"
    ]


async def test_find_names_regex_matches_multiple(obj: ObjTree):
    assert sorted(obj.find_names(".*test_(one|two)")) == [
        "sensor.test_one",
        "sensor.test_two",
    ]


async def test_find_paths_regex_combines_with_domain_filter(obj: ObjTree):
    assert sorted(obj.find_paths(".*test_one", domain="sensor")) == [
        "/test/sensor/test_one",
    ]
    assert list(obj.find_paths(".*test_one", domain="light")) == []


async def test_find_paths_invalid_regex_raises(obj: ObjTree):
    with pytest.raises(re.error):
        list(obj.find_paths("/test/sensor/["))


def _action(tree: ObjTree, path: str) -> Action:
    found = tree[path]
    assert isinstance(found, Action)
    return found


def _subtree(tree: ObjTree, path: str) -> ObjTree:
    found = tree[path]
    assert isinstance(found, ObjTree)
    return found


@pytest.fixture
def calls(hass: HomeAssistant, obj: ObjTree) -> list[ServiceCall]:
    """Two actions of the `test` integration, which also has the sensors,
    and one of an integration with nothing else - each call kept."""
    made: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        made.append(call)

    async def answer(call: ServiceCall) -> ServiceResponse:
        made.append(call)
        return {"said": call.data["word"]}

    hass.services.async_register("test", "poke", record)
    hass.services.async_register(
        "test", "ask", answer, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register("lonely", "wave", record)
    return made


async def test_actions_are_a_level_beside_an_integrations_domains(
    obj: ObjTree, calls: list[ServiceCall]
):
    # an integration with only actions is in the tree for them
    assert list(obj.keys()) == ["lonely", "test"]
    assert list(_subtree(obj, "/test").keys()) == ["action", "sensor"]
    assert list(_subtree(obj, "/test/action").keys()) == ["ask", "poke"]
    assert repr(obj["/test/action"]) == "<obj:/test/action: 2 actions>"
    with pytest.raises(KeyError):
        obj["/test/action/nope"]


async def test_an_action_can_be_looked_at_and_called(
    hass: HomeAssistant, obj: ObjTree, calls: list[ServiceCall]
):
    poke = _action(obj, "/test/action/poke")

    assert repr(poke) == "<action test.poke>"
    assert poke.action_id == "test.poke"
    assert poke.supports_response == "none"
    assert _action(_subtree(obj, "/test"), "action.ask").supports_response == "only"
    # nothing describes it yet
    assert obj.show("/test/action/poke") == {
        "action": "test.poke",
        "fields": {},
        "supports_response": "none",
    }
    async_set_service_schema(
        hass,
        "test",
        "poke",
        {
            "name": "Poke",
            "description": "Pokes it.",
            "fields": {
                "hard": {"required": True},
                "more": {"collapsed": True, "fields": {"times": {"example": 2}}},
            },
        },
    )
    assert poke.name == "Poke"
    # a field in a section of the form is listed with the rest
    assert poke.fields == {"hard": {"required": True}, "times": {"example": 2}}
    assert await poke.describe() == {
        "name": "Poke",
        "description": "Pokes it.",
        "fields": poke.fields,
    }
    assert obj.show("/test/action/poke") == {
        "action": "test.poke",
        "name": "Poke",
        "description": "Pokes it.",
        "fields": poke.fields,
        "supports_response": "none",
    }

    assert await poke(target={"entity_id": "sensor.test_one"}, hard=True) is None
    assert await _action(obj, "/test/action/ask")(word="hello") == {"said": "hello"}

    assert [(c.domain, c.service, dict(c.data)) for c in calls] == [
        ("test", "poke", {"hard": True, "entity_id": "sensor.test_one"}),
        ("test", "ask", {"word": "hello"}),
    ]


async def test_actions_are_the_same_in_api_mode(obj: ObjTree, calls: list[ServiceCall]):
    obj.mode("api")

    assert _action(obj, "/test/action/poke").action_id == "test.poke"


async def test_find_goes_through_actions_with_everything_else(
    obj: ObjTree, calls: list[ServiceCall]
):
    assert sorted(obj.find_names()) == [
        "lonely.wave",
        "sensor.test_one",
        "sensor.test_two",
        "test.ask",
        "test.poke",
    ]
    assert sorted(obj.find_paths("/test")) == [
        "/test/action/ask",
        "/test/action/poke",
        "/test/sensor/test_one",
        "/test/sensor/test_two",
    ]
    assert sorted(obj.find_names(domain="sensor")) == [
        "sensor.test_one",
        "sensor.test_two",
    ]
    assert sorted(obj.find_names(domain="action", platform="lonely")) == ["lonely.wave"]
    assert sorted(obj.find_paths("/(test|lonely)/action/.*e$")) == [
        "/lonely/action/wave",
        "/test/action/poke",
    ]
    assert list(_subtree(obj, "/lonely/action").find_paths()) == ["/wave"]
    raw: Any = list(obj.find("/lonely/action", raw=True))
    assert [d["action"] for d in raw] == ["lonely.wave"]


async def test_find_can_be_kept_to_one_object_type(
    obj: ObjTree, calls: list[ServiceCall]
):
    assert all(isinstance(e, MockEntity) for e in obj.find(object_type="entity"))
    assert sorted(obj.find_names(platform="test", object_type="entity")) == [
        "sensor.test_one",
        "sensor.test_two",
    ]
    assert sorted(obj.find_names(object_type="action")) == [
        "lonely.wave",
        "test.ask",
        "test.poke",
    ]
    assert len(list(obj.find(object_type=["entity", "action"]))) == 5
    # devices are not in the tree yet
    assert list(obj.find(object_type="device")) == []
    with pytest.raises(ValueError, match="object_type must be one of"):
        obj.find(object_type="helper")


async def test_key_completions_are_paths_below_and_entity_ids_at_the_root(
    obj: ObjTree,
):
    keys = obj._ipython_key_completions_()
    assert {"/test", "/test/sensor", "/test/sensor/test_one"} <= set(keys)
    assert {"sensor.test_one", "sensor.test_two"} <= set(keys)
    assert obj["/test"]._ipython_key_completions_() == [
        "/sensor",
        "/sensor/test_one",
        "/sensor/test_two",
    ]
    for key in keys:
        obj[key]
