"""Tests for API client mode's obj tree (ApiObjTree), against a fake Client - no
real Home Assistant needed. Payload shapes mirror what get_states and the
config/*_registry/list websocket commands actually return."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

from homeassistant_repl.api_objtree import ApiAction, ApiEntity, ApiObjTree, Cache


class FakeClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def call(self, type_: str, **payload: Any) -> Any:
        self.calls.append(type_)
        self.sent.append((type_, payload))
        if type_ == "get_services":  # none, unless a test has some
            return self.responses.get(type_, {})
        return self.responses[type_]


STATES = [
    {
        "entity_id": "light.kitchen_lights",
        "state": "on",
        "attributes": {"brightness": 200, "friendly_name": "Kitchen Lights"},
    },
    {"entity_id": "light.lounge_lamp", "state": "off", "attributes": {}},
    {"entity_id": "media_player.kitchen_show", "state": "idle", "attributes": {}},
    {"entity_id": "sensor.outside_temp", "state": "12.3", "attributes": {"unit": "C"}},
]

ENTITY_REGISTRY = [
    {
        "entity_id": "light.kitchen_lights",
        "platform": "hue",
        "area_id": None,
        "device_id": "dev_hue",
        "labels": ["lbl_important"],
        "unique_id": "uid1",
        "name": None,
        "created_at": 1700000000.0,
        "modified_at": 1700000000.0,
        "aliases": [],
        "options": {},
    },
    {
        "entity_id": "light.lounge_lamp",
        "platform": "hue",
        "area_id": "area_lounge",
        "device_id": None,
        "labels": [],
        "unique_id": "uid2",
    },
    {
        "entity_id": "media_player.kitchen_show",
        "platform": "alexa_devices",
        "area_id": "area_kitchen",
        "device_id": None,
        "labels": [],
        "unique_id": "uid3",
    },
    {
        "entity_id": "sensor.outside_temp",
        "platform": "demo",
        "area_id": None,
        "device_id": None,
        "labels": [],
        "unique_id": "uid4",
    },
]

DEVICE_REGISTRY = [{"id": "dev_hue", "area_id": "area_kitchen"}]
AREA_REGISTRY = [
    {"area_id": "area_kitchen", "name": "Kitchen"},
    {"area_id": "area_lounge", "name": "Lounge"},
]
LABEL_REGISTRY = [{"label_id": "lbl_important", "name": "Important"}]


@pytest.fixture
async def tree() -> ApiObjTree:
    client = FakeClient({
        "get_states": STATES,
        "config/entity_registry/list": ENTITY_REGISTRY,
        "config/device_registry/list": DEVICE_REGISTRY,
        "config/area_registry/list": AREA_REGISTRY,
        "config/label_registry/list": LABEL_REGISTRY,
    })
    cache = Cache(client, ttl=30)
    await cache.refresh()
    return ApiObjTree(cache)


async def test_root_keys_are_integrations(tree: ApiObjTree):
    assert list(tree.keys()) == ["/alexa_devices", "/demo", "/hue"]


async def test_domain_level_keys(tree: ApiObjTree):
    hue = tree["/hue"]
    assert isinstance(hue, ApiObjTree)
    assert list(hue.keys()) == ["/light"]


async def test_leaf_lookup_returns_api_entity(tree: ApiObjTree):
    entity = tree["/hue/light/kitchen_lights"]
    assert isinstance(entity, ApiEntity)
    assert entity.state == "on"
    assert entity.state_attributes == {"brightness": 200}
    # friendly_name is promoted to .name, not left sitting in state_attributes:
    assert entity.name == "Kitchen Lights"


async def test_name_falls_back_to_none_without_friendly_name(tree: ApiObjTree):
    lounge_lamp = tree["/hue/light/lounge_lamp"]
    assert isinstance(lounge_lamp, ApiEntity)
    assert lounge_lamp.name is None


async def test_bare_entity_id_lookup(tree: ApiObjTree):
    kitchen_lights = tree["light.kitchen_lights"]
    assert isinstance(kitchen_lights, ApiEntity)
    assert kitchen_lights.entity_id == "light.kitchen_lights"


async def test_bare_entity_id_lookup_scoped_to_integration(tree: ApiObjTree):
    hue = tree["/hue"]
    assert isinstance(hue, ApiObjTree)
    kitchen_lights = hue["light.kitchen_lights"]
    assert isinstance(kitchen_lights, ApiEntity)
    assert kitchen_lights.entity_id == "light.kitchen_lights"
    assert kitchen_lights is tree["light.kitchen_lights"]


async def test_bare_entity_id_lookup_wrong_integration_fails(tree: ApiObjTree):
    # media_player.kitchen_show belongs to alexa_devices, not hue.
    hue = tree["/hue"]
    assert isinstance(hue, ApiObjTree)
    with pytest.raises(KeyError):
        hue["media_player.kitchen_show"]


async def test_missing_intermediate_path_raises(tree: ApiObjTree):
    with pytest.raises(KeyError):
        tree["/hue/sensor"]  # hue has no sensors


async def test_find_paths_flat_no_filters(tree: ApiObjTree):
    assert sorted(tree.find_paths()) == [
        "/alexa_devices/media_player/kitchen_show",
        "/demo/sensor/outside_temp",
        "/hue/light/kitchen_lights",
        "/hue/light/lounge_lamp",
    ]


async def test_find_paths_by_area_inherits_from_device(tree: ApiObjTree):
    # light.kitchen_lights has no area of its own, but its device (dev_hue) does.
    assert sorted(tree.find_paths(area="Kitchen")) == [
        "/alexa_devices/media_player/kitchen_show",
        "/hue/light/kitchen_lights",
    ]


async def test_find_paths_by_area_id_too(tree: ApiObjTree):
    assert sorted(tree.find_paths(area="area_lounge")) == ["/hue/light/lounge_lamp"]


async def test_find_paths_by_platform(tree: ApiObjTree):
    assert sorted(tree.find_paths(platform="hue")) == [
        "/hue/light/kitchen_lights",
        "/hue/light/lounge_lamp",
    ]


async def test_find_paths_by_label_name(tree: ApiObjTree):
    assert list(tree.find_paths(label="Important")) == ["/hue/light/kitchen_lights"]


async def test_find_paths_scoped_by_path(tree: ApiObjTree):
    assert sorted(tree.find_paths("/hue")) == [
        "/hue/light/kitchen_lights",
        "/hue/light/lounge_lamp",
    ]


async def test_find_paths_by_domain_across_integrations(tree: ApiObjTree):
    assert sorted(tree.find_paths(domain="light")) == [
        "/hue/light/kitchen_lights",
        "/hue/light/lounge_lamp",
    ]


async def test_find_paths_filters_and_together(tree: ApiObjTree):
    # domain=light AND platform=demo: no light is on the demo platform here.
    assert list(tree.find_paths(domain="light", platform="demo")) == []


async def test_find_paths_accepts_a_regex_prefix(tree: ApiObjTree):
    # "." and "*" make this a regex, not a literal 3-segment path.
    assert sorted(tree.find_paths("/hue/light/kitchen.*")) == [
        "/hue/light/kitchen_lights",
    ]


async def test_find_names_regex_can_cross_integrations(tree: ApiObjTree):
    # A plain /integration/domain/object_id prefix can't express this -
    # kitchen_lights (hue) and kitchen_show (alexa_devices) are unrelated
    # integrations, only a pattern over the whole path can match both.
    assert sorted(tree.find_names(".*kitchen.*")) == [
        "light.kitchen_lights",
        "media_player.kitchen_show",
    ]


async def test_find_paths_regex_combines_with_platform_filter(tree: ApiObjTree):
    assert sorted(tree.find_paths(".*kitchen.*", platform="hue")) == [
        "/hue/light/kitchen_lights",
    ]


async def test_find_paths_invalid_regex_raises(tree: ApiObjTree):
    with pytest.raises(re.error):
        list(tree.find_paths("/hue/light/["))


async def test_find_yields_bare_entities(tree: ApiObjTree):
    [entity] = list(tree.find(domain="light", platform="hue", area="Kitchen"))
    assert isinstance(entity, ApiEntity)
    assert entity.entity_id == "light.kitchen_lights"
    assert entity.state == "on"
    # Same object indexing would give you - not a separate lookup/copy.
    assert entity is tree["/hue/light/kitchen_lights"]


async def test_find_raw_yields_show_shaped_dict_not_a_pair(tree: ApiObjTree):
    [raw] = list(tree.find(domain="light", platform="hue", area="Kitchen", raw=True))
    assert isinstance(raw, dict)
    assert raw == tree.show("/hue/light/kitchen_lights")
    assert raw["entity_id"] == "light.kitchen_lights"
    assert raw["state"] == "on"
    assert raw["name"] == "Kitchen Lights"


async def test_find_names_vs_find_paths_unaffected_by_raw(tree: ApiObjTree):
    # find_paths()/find_names() never pass raw through - they only need
    # .path/.entity_id, which stay simple strings either way.
    assert list(tree.find_names(area="Kitchen", platform="hue")) == [
        "light.kitchen_lights"
    ]
    assert list(tree.find_paths(area="Kitchen", platform="hue")) == [
        "/hue/light/kitchen_lights"
    ]


async def test_find_names_returns_entity_ids(tree: ApiObjTree):
    assert sorted(tree.find_names(platform="hue")) == [
        "light.kitchen_lights",
        "light.lounge_lamp",
    ]


async def test_find_names_vs_find_paths(tree: ApiObjTree):
    # Same filter, two different string forms of the same match.
    assert list(tree.find_names(area="Kitchen", platform="hue")) == [
        "light.kitchen_lights"
    ]
    assert list(tree.find_paths(area="Kitchen", platform="hue")) == [
        "/hue/light/kitchen_lights"
    ]


async def test_find_unknown_area_raises_immediately(tree: ApiObjTree):
    with pytest.raises(KeyError):
        tree.find(area="Nonexistent")  # must raise before iteration, not during


async def test_passing_an_entity_instead_of_a_path_raises_typeerror(tree: ApiObjTree):
    # Regression: tree.show(tree["/hue/light/kitchen_lights"]) used to crash
    # deep inside parse_path with a confusing AttributeError.
    entity = tree["/hue/light/kitchen_lights"]
    with pytest.raises(TypeError):
        tree.show(entity)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        # must raise immediately, not only once iterated
        tree.find(entity)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        tree[entity]  # type: ignore[index]  # ty: ignore[invalid-argument-type]


async def test_show_cleans_and_merges(tree: ApiObjTree):
    result = tree.show("/hue/light/kitchen_lights")
    assert result["state"] == "on"
    assert result["state_attributes"] == {"brightness": 200}
    # "name" is the live-equivalent display name (from friendly_name), not
    # the registry's own (here None) override field:
    assert result["name"] == "Kitchen Lights"
    assert result["platform"] == "hue"
    assert result["labels"] == ["lbl_important"]
    # None / empty list dropped, but an empty dict is kept (only None and
    # empty-collection-standing-in-for-a-set are dropped, same as live mode):
    assert "aliases" not in result
    assert result["options"] == {}
    # timestamp converted to a local ISO 8601 string, not left as a float:
    assert isinstance(result["created_at"], str)
    assert "T" in result["created_at"]


async def test_ordered_views_support_indexing(tree: ApiObjTree):
    keys = tree.keys()
    assert keys[0] == "/alexa_devices"
    assert keys[-1] == "/hue"
    assert list(reversed(keys)) == ["/hue", "/demo", "/alexa_devices"]


async def test_reset_cache_marks_stale(tree: ApiObjTree):
    assert not tree.cache.is_stale()
    tree.reset_cache()
    assert tree.cache.is_stale()


async def test_entity_reads_like_a_live_entity(tree: ApiObjTree):
    """The spellings that work on a real Entity work here too."""
    entity = tree["/hue/light/kitchen_lights"]
    assert isinstance(entity, ApiEntity)

    # platform: still the plain string, and the live EntityPlatform's attributes
    assert entity.platform == "hue"
    assert entity.platform.platform_name == "hue"
    assert entity.platform.domain == "light"
    assert json.dumps(entity.platform) == '"hue"'
    assert copy.deepcopy(entity.platform).domain == "light"

    # registry_entry: attribute access as on a RegistryEntry, and still a dict
    entry = entity.registry_entry
    assert entry.platform == entry["platform"] == "hue"
    assert entity.unique_id == entry.get("unique_id")
    with pytest.raises(AttributeError):
        entry.no_such_field  # noqa: B018

    assert entity.available is True
    assert entity.enabled is True
    assert entity.assumed_state is False
    assert entity.device_class == entity.state_attributes.get("device_class")
    assert entity.icon == entity.state_attributes.get("icon")


async def test_entity_with_no_state_is_not_available():
    cache = Cache(
        FakeClient({
            "get_states": [],
            "config/entity_registry/list": [
                {**ENTITY_REGISTRY[0], "disabled_by": "user"}
            ],
            "config/device_registry/list": DEVICE_REGISTRY,
            "config/area_registry/list": AREA_REGISTRY,
            "config/label_registry/list": LABEL_REGISTRY,
        }),
        ttl=30,
    )
    await cache.refresh()

    [entity] = cache.entities.values()

    assert entity.state is None
    assert entity.available is False
    assert entity.enabled is False


# As get_services gives them: `response` only on an action that answers.
SERVICES = {
    "hue": {
        "activate_scene": {
            "name": "Activate scene",
            "description": "Activates a Hue scene.",
            "fields": {"transition": {"name": "Transition"}},
        }
    },
    "mqtt": {
        "publish": {
            "name": "Publish",
            "description": "Publishes a message to an MQTT topic.",
            "fields": {"topic": {"required": True}, "payload": {}},
        },
        "dump": {"name": "Dump", "description": "", "fields": {}},
    },
    "weather": {
        "get_forecasts": {
            "name": "Get forecasts",
            "description": "",
            "fields": {},
            "response": {"optional": False},
        }
    },
}


# The frontend's own names for an action and its fields, where get_services
# has none - which is most of them.
TRANSLATIONS = {
    "component.mqtt.services.dump.name": "Export",
    "component.mqtt.services.dump.description": "Writes all messages on a topic.",
    "component.mqtt.services.publish.name": "not used: get_services has one",
    "component.mqtt.services.publish.fields.topic.name": "Topic",
    "component.mqtt.services.publish.fields.topic.description": "Topic to publish to.",
}


@pytest.fixture
async def client() -> FakeClient:
    return FakeClient({
        "get_states": STATES,
        "config/entity_registry/list": ENTITY_REGISTRY,
        "config/device_registry/list": DEVICE_REGISTRY,
        "config/area_registry/list": AREA_REGISTRY,
        "config/label_registry/list": LABEL_REGISTRY,
        "get_services": SERVICES,
        "get_config": {"language": "en"},
        "frontend/get_translations": {"resources": TRANSLATIONS},
        "call_service": {"context": {}, "response": {"weather.home": {"forecast": []}}},
    })


def _action(tree: ApiObjTree, path: str) -> ApiAction:
    found = tree[path]
    assert isinstance(found, ApiAction)
    return found


def _subtree(tree: ApiObjTree, path: str) -> ApiObjTree:
    found = tree[path]
    assert isinstance(found, ApiObjTree)
    return found


@pytest.fixture
async def actions(client: FakeClient) -> ApiObjTree:
    cache = Cache(client, ttl=30)
    await cache.refresh()
    return ApiObjTree(cache)


async def test_actions_are_a_level_beside_an_integrations_domains(actions: ApiObjTree):
    # an integration with only actions is in the tree for them
    assert list(actions.keys()) == [
        "/alexa_devices",
        "/demo",
        "/hue",
        "/mqtt",
        "/weather",
    ]
    assert list(_subtree(actions, "/hue").keys()) == ["/action", "/light"]
    assert list(_subtree(actions, "/mqtt/action").keys()) == ["/dump", "/publish"]
    assert repr(actions["/mqtt/action"]) == "<obj:/mqtt/action: 2 actions (api)>"
    with pytest.raises(KeyError):
        actions["/mqtt/action/nope"]
    with pytest.raises(KeyError):
        actions["/demo/action"]


async def test_an_action_is_described(actions: ApiObjTree):
    action = _action(actions, "/mqtt/action/publish")

    assert repr(action) == "<action mqtt.publish>"
    assert action.action_id == "mqtt.publish"
    assert action.name == "Publish"
    assert list(action.fields) == ["topic", "payload"]
    # the frontend's names for it, where get_services has none
    assert action.fields["topic"] == {
        "name": "Topic",
        "description": "Topic to publish to.",
        "required": True,
    }
    assert (
        _action(actions, "/mqtt/action/dump").description
        == "Writes all messages on a topic."
    )
    assert action.supports_response == "none"
    assert _action(actions, "/weather/action/get_forecasts").supports_response == "only"
    assert (await action.describe())["description"].startswith("Publishes")
    assert actions.show("/mqtt/action/publish") == {
        "action": "mqtt.publish",
        "name": "Publish",
        "description": "Publishes a message to an MQTT topic.",
        "fields": {
            "topic": {
                "name": "Topic",
                "description": "Topic to publish to.",
                "required": True,
            },
            "payload": {},
        },
        "supports_response": "none",
    }


async def test_calling_an_action_sends_a_call_service(
    actions: ApiObjTree, client: FakeClient
):
    publish = _action(actions, "/mqtt/action/publish")
    forecasts = _action(actions, "/weather/action/get_forecasts")

    assert await publish(topic="shed/fan", payload="on") is None
    assert await forecasts(target={"entity_id": "weather.home"}, type="daily") == {
        "weather.home": {"forecast": []}
    }

    assert client.sent[-2:] == [
        (
            "call_service",
            {
                "domain": "mqtt",
                "service": "publish",
                "service_data": {"topic": "shed/fan", "payload": "on"},
                "return_response": False,
            },
        ),
        (
            "call_service",
            {
                "domain": "weather",
                "service": "get_forecasts",
                "service_data": {"type": "daily"},
                "return_response": True,
                "target": {"entity_id": "weather.home"},
            },
        ),
    ]


async def test_find_goes_through_actions_with_everything_else(actions: ApiObjTree):
    assert sorted(actions.find_names()) == [
        "hue.activate_scene",
        "light.kitchen_lights",
        "light.lounge_lamp",
        "media_player.kitchen_show",
        "mqtt.dump",
        "mqtt.publish",
        "sensor.outside_temp",
        "weather.get_forecasts",
    ]
    assert sorted(actions.find_paths("/hue")) == [
        "/hue/action/activate_scene",
        "/hue/light/kitchen_lights",
        "/hue/light/lounge_lamp",
    ]
    assert sorted(actions.find_names(platform="hue", domain="action")) == [
        "hue.activate_scene"
    ]
    assert sorted(actions.find_names(domain="light")) == [
        "light.kitchen_lights",
        "light.lounge_lamp",
    ]
    assert sorted(actions.find_names("/mqtt/action")) == ["mqtt.dump", "mqtt.publish"]
    assert sorted(actions.find_paths("/.*/(action|light)/(publish|lounge.*)")) == [
        "/hue/light/lounge_lamp",
        "/mqtt/action/publish",
    ]
    assert sorted(_subtree(actions, "/mqtt/action").find_paths()) == [
        "/dump",
        "/publish",
    ]
    # an action has no area or label to be found by
    assert sorted(actions.find_names(area="Kitchen")) == [
        "light.kitchen_lights",
        "media_player.kitchen_show",
    ]
    raw: Any = list(actions.find("/hue/action", raw=True))
    assert [d["action"] for d in raw] == ["hue.activate_scene"]


async def test_find_can_be_kept_to_one_object_type(actions: ApiObjTree):
    assert all(isinstance(e, ApiEntity) for e in actions.find(object_type="entity"))
    assert sorted(actions.find_names(platform="hue", object_type="entity")) == [
        "light.kitchen_lights",
        "light.lounge_lamp",
    ]
    assert sorted(actions.find_names("/mqtt", object_type="action")) == [
        "mqtt.dump",
        "mqtt.publish",
    ]
    assert len(list(actions.find(object_type=["entity", "action"]))) == 8
    # devices are not in the tree yet
    assert list(actions.find(object_type="device")) == []
    with pytest.raises(ValueError, match="object_type must be one of"):
        actions.find(object_type="helper")


async def test_key_completions_are_paths_below_and_entity_ids_at_the_root(
    tree: ApiObjTree,
):
    keys = tree._ipython_key_completions_()
    assert {"/hue", "/hue/light", "/hue/light/kitchen_lights"} <= set(keys)
    assert "light.kitchen_lights" in keys
    hue = tree["/hue"]
    assert isinstance(hue, ApiObjTree)
    assert hue._ipython_key_completions_() == [
        "/light",
        "/light/kitchen_lights",
        "/light/lounge_lamp",
    ]
    # Each one is a key that works.
    for key in keys:
        tree[key]


async def test_registry_entry_keys_complete_as_attributes(tree: ApiObjTree):
    entry = tree["light.kitchen_lights"].registry_entry
    assert "unique_id" in dir(entry)
    assert "items" in dir(entry)
