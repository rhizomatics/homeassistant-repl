# API Mode

This is the simplest mode, and easiest to get started with. It doesn't require any server side components, only a token. Since it uses the standard Home Assistant APIs, it doesn't offer the same security and footgun potential as live mode. Its also simple in-process Python, without the fancy blended operation of live mode.

Its aimed to be as code compatible with `live`, so you can use `obj` code in either, and switch to `live` mode if you need more direct entity access without having to learn new syntax, and being able to reuse your existing plugins.

## Built-ins

The two special variables in `api` mode are:

* `obj` - the [Object Tree](../obj_tree.md) exposed as a dictionary object and common methods, with the source being the Home Assistant API. It has each integration's [actions](../obj_tree.md#actions) as well as the entities, to look at or call
* `hass_api` - the Home Assistant web socket / REST API exposed using the [homeassistant-api](https://pypi.org/project/HomeAssistant-API/) library

The objects returned by `obj` are of class `ApiEntity`, and combines the `Entity` class from Home Assistant, with `RegistryEntity` since things of common use are split across both.

### Example ApiEntity
```python
>>> obj["sensor.sun_next_rising"]
ApiEntity(
    entity_id='sensor.sun_next_rising',
    platform='sun',
    domain='sensor',
    object_id='sun_next_rising',
    state='2026-10-09T06:34:50+00:00',
    name='Sun Next rising',
    state_attributes={'device_class': 'timestamp'},
    area_id=None,
    labels=frozenset(),
    registry_entry={
        'area_id': None,
        'categories': {},
        'config_entry_id': 'dc912d263e164495aede7834731fa675',
        'config_subentry_id': None,
        'created_at': 0.0,
        'device_id': 'd9df6cad924f2747eab95d2b414265f0',
        'disabled_by': None,
        'entity_category': 'diagnostic',
        'entity_id': 'sensor.sun_next_rising',
        'has_entity_name': True,
        'hidden_by': None,
        'icon': None,
        'id': 'f1e460ed2f879b80cbf5e2cc6b8e3c5e',
        'labels': [],
        'modified_at': 1770236490.999007,
        'name': None,
        'next_name_part': 'device',
        'options': {
            'cloud.alexa': {'should_expose': False},
            'cloud.google_assistant': {'should_expose': False},
            'conversation': {'should_expose': False}
        },
        'original_name': 'Next rising',
        'platform': 'sun',
        'translation_key': 'next_rising',
        'unique_id': 'dc912d263e164495aede7834731fa675-next_rising'
    }
)
```

In `live` mode the entities returned are the actual `Entity` objects from Home Assistant, with their full set of methods and other attributes.

### Code That Works in Both Modes

An `ApiEntity` uses the real `Entity` names wherever it has the data, so the same expression reads the same in `api` and `live` mode:

| Expression | In `api` mode it comes from |
| ---------- | --------------------------- |
| `entity_id`, `state`, `name`, `state_attributes` | The entity's state |
| `registry_entry.unique_id`, `registry_entry.options`, and so on | The entity registry. Also a dict, so `registry_entry["unique_id"]` works in `api` mode |
| `platform.platform_name`, `platform.domain` | The entity registry. `platform` is also the plain string, so `platform == "mqtt"` works in `api` mode |
| `unique_id`, `enabled`, `entity_category`, `has_entity_name`, `translation_key` | The entity registry |
| `device_class`, `unit_of_measurement`, `icon`, `entity_picture`, `supported_features`, `assumed_state`, `attribution` | The state's attributes |
| `available` | False when the state is `unavailable`, or there is none |

```python
{
    e.entity_id: e.registry_entry.unique_id
    for e in obj.find(platform="mqtt", object_type="entity")
    if e.available
}
```

`domain`, `object_id`, `area_id` and `labels` are extras that a real `Entity` doesn't have. The spelling that works on both is `platform.domain`, `registry_entry.area_id` and `registry_entry.labels`.

Some differences remain:

- Values inside `registry_entry` are as the API sends them: `labels` is a list where the real one is a set, and `created_at` and `modified_at` are numbers where the real ones are datetimes
- `state_attributes` holds everything the state publishes, including `device_class` and `unit_of_measurement`, which a real `Entity` keeps out of it
- There is no `device_entry`, `device_info`, `extra_state_attributes` or `capability_attributes`, and no methods

### Home Assistant API

The [homeassistant-api](https://pypi.org/project/HomeAssistant-API/) project
has native Python objects that wrap around the JSON results from Home Assistant WebSockets and REST API calls.
These can be accessed via `hass_api`, and mixed with the results and calls of `obj`, and works identically in `live` and `api` mode.

The API access is read/write, so can be used for anything that the user who issued the token can do.

```python
hass_api.trigger_service("switch", "turn_off", entity_id="switch.living_room_light")
```

An action can also be called straight from the object tree, which works the same in `live` mode:

```python
obj["/switch/action/turn_off"](target={"entity_id": "switch.living_room_light"})
```

Since everything is plain Python, these can be combined together in procedural or one-liner comprehension type code, and use other libraries you've imported.

```python
for e in obj.find_names(".*light.*", domain="switch"):
    hass_api.trigger_service("switch", "turn_off", entity_id=e)
```
### Getting The Built-ins Back

`obj` and `hass_api` are ordinary names, as are `show`, `help`, `MODE` and `SERVER`, so assigning to one replaces it for the rest of the session - `obj = 5`, or a `for obj in things:` loop. `rebind()` puts them all back, and runs the [plugins](../configuration/plugins.md) again.

```python
>>> obj = obj["sensor.sun_next_rising"]    # `obj` is now that one entity
>>> rebind()                               # `obj` is the object tree again
>>> rebind(plugins=False)                  # the same, without running the plugins
```

Nothing else in the session is touched: variables of your own stay as they are, apart from any a plugin assigns. It works the same way in `live` mode.
