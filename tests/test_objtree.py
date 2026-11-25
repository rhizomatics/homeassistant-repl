"""Tests for ObjTree's `mode("api"/"live")` switch, against a real (if
minimal) entity via pytest-homeassistant-custom-component - unlike
test_session.py's "fake-hass" string, this needs a genuine live Entity and
entity registry entry to exercise the api/live branches honestly."""

from __future__ import annotations

import copy
import pickle
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.setup import async_setup_component

from custom_components.ha_repl_server.objtree import (
    Action,
    ApiEntity,
    ApiPlatform,
    ApiRegistryEntry,
    ObjTree,
    _clean,
    _Found,
)
from tests.ha_testing import MockConfigEntry, MockEntity, setup_test_component_platform


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
    below = obj["/test"]
    assert isinstance(below, ObjTree)
    assert below._ipython_key_completions_() == [
        "/sensor",
        "/sensor/test_one",
        "/sensor/test_two",
    ]
    for key in keys:
        obj[key]


async def test_keys_that_lead_nowhere_are_key_errors(obj: ObjTree):
    with pytest.raises(TypeError, match="key must be a str, not int"):
        obj[5]  # type: ignore[index]  # ty: ignore[invalid-argument-type]
    for missing in (
        "sensor.nope",  # a bare entity id there's no entity for
        "/a/b/c/d",  # not a path
        "/nothing",  # an integration with nothing in the tree
        "/test/sensor/nope",
        "/other/sensor/test_one",  # an entity, but not of that integration
    ):
        with pytest.raises(KeyError):
            obj[missing]
    with pytest.raises(KeyError):
        _subtree(obj, "/test")["sensor/test_one/x"]  # four parts, scoped
    obj.mode("api")
    for missing in ("sensor.nope", "/test/sensor/nope", "/other/sensor/test_one"):
        with pytest.raises(KeyError):
            obj[missing]


async def test_find_and_show_refuse_paths_that_lead_nowhere(
    obj: ObjTree, calls: list[ServiceCall]
):
    with pytest.raises(KeyError):
        list(obj.find_paths("/a/b/c/d"))
    with pytest.raises(KeyError):
        list(_subtree(obj, "/test").find_paths("/sensor/test_one/x"))
    for bad in ("/a/b/c/d", "/test", "/test/action/nope", "/test/sensor/nope"):
        with pytest.raises(KeyError):
            obj.show(bad)


async def test_show_and_raw_find_give_an_entitys_registry_entry_and_state(
    obj: ObjTree,
):
    shown = obj.show("/test/sensor/test_one")
    assert shown["entity_id"] == "sensor.test_one"
    assert shown["name"] == "Test One"
    assert shown["state"] == "42"
    assert "_cache" not in shown
    raw: Any = list(obj.find("/test/sensor", raw=True))
    assert sorted(d["entity_id"] for d in raw) == ["sensor.test_one", "sensor.test_two"]


async def test_clean_drops_empties_and_renders_datetimes():
    when = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    cleaned = _clean({
        "at": when,
        "gone": None,
        "none": set(),
        "inner": {"x": None, "y": 1},
    })
    assert set(cleaned) == {"at", "inner"}
    assert cleaned["at"].startswith("2026-11-25")
    assert cleaned["inner"] == {"y": 1}


async def test_find_by_area_label_and_device_area(hass: HomeAssistant, obj: ObjTree):
    shed = ar.async_get(hass).async_create("Shed")
    house = ar.async_get(hass).async_create("House")
    outdoor = lr.async_get(hass).async_create("Outdoor")
    entities = er.async_get(hass)
    entities.async_update_entity(
        "sensor.test_one", area_id=shed.id, labels={outdoor.label_id}
    )
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "box")}
    )
    dr.async_get(hass).async_update_device(device.id, area_id=house.id)
    # No area of its own: it's in its device's
    entities.async_update_entity("sensor.test_two", device_id=device.id)

    assert list(obj.find_names(area="Shed")) == ["sensor.test_one"]
    assert list(obj.find_names(area=house.id)) == ["sensor.test_two"]
    assert list(obj.find_names(label="Outdoor")) == ["sensor.test_one"]
    assert list(obj.find_names(platform="other")) == []
    assert list(obj.find_names(domain="light")) == []
    with pytest.raises(KeyError, match="no such area: 'Barn'"):
        obj.find_names(area="Barn")
    obj.mode("api")
    one = obj["sensor.test_one"]
    two = obj["sensor.test_two"]
    assert isinstance(one, ApiEntity) and isinstance(two, ApiEntity)
    assert (one.area_id, two.area_id) == (shed.id, house.id)
    assert one.labels == frozenset({outdoor.label_id})


async def test_an_api_entity_reads_like_a_live_one(obj: ObjTree):
    obj.mode("api")
    entity = obj["sensor.test_one"]
    assert isinstance(entity, ApiEntity)
    # What the state and registry entry can answer, under the live names
    assert entity.device_class is None
    assert entity.unit_of_measurement is None
    assert entity.icon is None
    assert entity.entity_picture is None
    assert entity.supported_features is None
    assert entity.assumed_state is False
    assert entity.attribution is None
    assert entity.entity_category is None
    assert entity.has_entity_name is False
    assert entity.translation_key is None


async def test_api_platform_and_registry_entry_are_their_plain_types_too():
    platform = ApiPlatform("mqtt", "light")
    assert platform == "mqtt" and platform.platform_name == "mqtt"
    for copied in (copy.copy(platform), pickle.loads(pickle.dumps(platform))):
        assert (copied, copied.domain) == ("mqtt", "light")
    entry = ApiRegistryEntry({"unique_id": "u1", "not an identifier": 1})
    assert entry.unique_id == "u1"
    with pytest.raises(AttributeError, match="nope"):
        entry.nope  # noqa: B018 - the attribute read is what's tested
    assert "unique_id" in dir(entry)
    assert "not an identifier" not in dir(entry)


async def test_views_are_sequences_and_found_reads_through(
    obj: ObjTree, calls: list[ServiceCall]
):
    keys = obj.keys()
    assert keys[0] == "lonely"
    assert list(reversed(keys)) == ["test", "lonely"]
    assert keys[:1] == ("lonely",)
    # Every key looks up, here at the root too, where they're bare names
    assert "lonely" in obj
    assert isinstance(obj["lonely"], ObjTree)
    assert [repr(v) for v in obj.values()] == [
        "<obj:/lonely: 1 domains>",
        "<obj:/test: 2 domains>",
    ]
    assert dict(obj.items()).keys() == {"lonely", "test"}
    entity = obj["sensor.test_one"]
    assert isinstance(entity, MockEntity)
    found = _Found("/test/sensor/test_one", entity)
    assert found.entity_id == "sensor.test_one"


async def test_an_action_says_what_it_checks_and_can_be_asked_for_its_answer(
    hass: HomeAssistant, obj: ObjTree, calls: list[ServiceCall]
):
    ask = _action(obj, "/test/action/ask")
    assert ask.schema is None
    assert ask.fields == {}
    assert await ask(word="hi", return_response=True) == {"said": "hi"}
    assert repr(obj) == "<obj: 2 integrations>"
    assert repr(_subtree(obj, "/test")) == "<obj:/test: 2 domains>"
    assert repr(_subtree(obj, "/test/sensor")) == "<obj:/test/sensor: 2 entities>"
