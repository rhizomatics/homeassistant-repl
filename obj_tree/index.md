# The Object Tree

All of the objects (entities and actions for now) are arranged in a giant tree, like a file system, exposed as the global variable `obj` and implemented as Python [Mapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping) object (which provides the [MappingView](https://docs.python.org/3/library/collections.abc.html#collections.abc.MappingView) views [ItemsView](https://docs.python.org/3/library/collections.abc.html#collections.abc.ItemsView) and [KeysView](https://docs.python.org/3/library/collections.abc.html#collections.abc.KeysView))

`obj` offers:

- Dictionary style access, using `[]`
- Raises `KeyError` if entity or sub-path doesn't exist
- Each level of the tree returns a sub-tree
- `keys()`,`values()`,`items()` of the sub-tree shows only that level
- An `OrderedView` is used rather than plain `MappingView` so can be accessed like a `list` and items are alphabetically organized
- `find()`
- Returns a flat iterable of the entire tree
- Optionally restrict by `platform`,`area`,`label`
- `show()`
- Dump the most useful info on an object to console
- [Actions](#actions)
- Each integration's actions, in an `action` level beside its entity domains, to look at or call

`help(obj)` gives a summary of all of this at the prompt.

All the usual Python tricks can of course also be used, iterators, comprehensions, classes, lambdas or a simple `len()`.

### `obj[]`

This allows dictionary ('Mapping') access to the object tree. It can also accept a simple entity name and bypass the tree structure altogether.

In the example tree below, the objects and subtrees of objects can be accessed like:

```python
>>> obj["/rflink/sensor/shed_temperature"].state  # prints out temperature
>>> obj[
    "/rflink/sensor/shed_temperature"
].state_attributes  # prints out additional attributes
>>> obj["/rflink"]  # the 'light','binary_sensor' and 'sensor' subtrees for rflink
>>> obj["/rflink/sensor"]  # all the sensors for rflink
>>> len(obj["/rflink/sensor"])  # count of rflink sensors in this example
>>> obj["sensor.shed_temperature"] # non-path simple entity name mode
```

##### Example Tree

```text
...
- mqtt
- rflink
  - light
    - staircase_ceiling
    - shed
  - switch
    - upstairs_pixie
  - binary_sensor
    - porch_pir
    - shed_door
  - sensor
    - shed_temperature
    - kitchen_humidity
...
```

Note

In the roadmap, there will be a visual Object Browser to view and select entities. For now, it is accessible only via Python code. It also may extend beyond entities, to things like areas, users, categories and devices.

#### `obj.find('..')`

Where the dictionary access gives a nested directory view of the object tree, `find` provides a flat iteration with no order guarantees, so its fast and simple and can be sorted the usual Python way if needed.

`find` functions can accept a full or partial path, or a regular expression.

`find` also has built in filters, to narrow the big list of objects by one or more `platform`,`domain`,`area`,`label`,`object_type` - each of these will take a single string or list of strings, and they can be combined to narrow down the list. `object_type` is `"entity"`, `"action"` or `"device"` - see [Finding Actions](#finding-actions).

```python
>>> {(o.entity_id, o.state) for o in obj.find(domain="binary_sensor")}
```

Or using a regular expression:

```python
>>> {(o.entity_id, o.state) for o in obj.find("/rflink/binary_sensor/*._leak")}
```

##### Raw Objects

In API Client mode, `find()` returns a local proxy for the remote class, normalized to look more like the same object you'd get in live mode. Switching `raw=True` will bypass this and you'll get the object untouched as it was received from the API.

#### `obj.find_paths(..)`

Identical to `obj.find()` except it only returns an iterable of the object paths rather than the objects themselves. Ideal for plugging into some logic that will then call `obj[path]` on each one.

```python
>>> list(obj.find(area="kitchen"))  # list names of all entities in kitchen
>>> sorted(obj.find(area=["kitchen", "shed"]))
>>> list(obj.find(".*_leak"))
```

#### `obj.find_names(..)`

Same as `obj.find_paths()` except it returns the object bare name, as it would appear in Home Assistant, e.g. `sensor.bathroom_humidity`

#### `obj.show(..)`

The `show` function will give a pretty version of an object where it knows how. For entities this means it combines the `RegistryEntry` and `Entity` information, drops some boring internal stuff, filters out all the `None` values and turns `datetime.datetime` structures into local date times.

```yaml
obj.show('/unifi/sensor/kitchen_wifi_cpu_utilization')
```

### Actions

An integration's actions - what Home Assistant used to call services - are in the tree with its entities, in an `action` level beside its entity domains. So `mqtt.publish` is at `/mqtt/action/publish`, and `light.turn_on` at `/light/action/turn_on`.

```text
- mqtt
  - action
    - dump
    - publish
    - reload
  - sensor
    - shed_temperature
```

```python
>>> obj["/mqtt"].keys()                 # 'action', then mqtt's entity domains
>>> obj["/mqtt/action"].keys()          # the names of mqtt's actions
>>> obj["/mqtt/action/publish"]         # one action
>>> obj.show("/mqtt/action/publish")    # its name, description and fields
```

An integration that has actions and no entities, such as `homeassistant` or `persistent_notification`, is in the tree for its actions alone.

#### Calling An Action

An action is called like a function, with the action's data as keyword arguments. `target` says what it is to act on, as it does in an automation.

```python
>>> obj["/mqtt/action/publish"](topic="shed/fan", payload="on")
>>> obj["/light/action/turn_on"](target={"entity_id": "light.shed"}, brightness=128)
>>> obj["/light/action/turn_off"](target={"area_id": "kitchen"})
>>> obj["/weather/action/get_forecasts"](entity_id="weather.home", type="daily")
```

The call waits for the action to finish. An action that answers with data, such as `weather.get_forecasts`, gives that back; for one where answering is optional, pass `return_response=True` to ask for it.

Warning

Calling an action does what it says - the light goes off, the message is published. This is the one part of `obj` that is not read-only, in API mode as much as in live mode.

#### What An Action Has

| Operation                       | Description                                                                |
| ------------------------------- | -------------------------------------------------------------------------- |
| `action_id`                     | The action as Home Assistant names it, e.g. `mqtt.publish`                 |
| `domain`, `service`             | The two halves of that                                                     |
| `name`, `description`, `fields` | Home Assistant's own description of the action, and of each field it takes |
| `supports_response`             | `none`, `optional` or `only` - whether calling it gives data back          |
| `describe()`                    | The name, description and fields as one dict                               |

`fields` has an entry for each field the action takes, with its name and description, whether it is `required`, an `example` where there is one, and the `selector` the frontend uses for it.

```python
>>> obj.show("/mqtt/action/publish")
>>> obj["/mqtt/action/publish"].fields.keys()
>>> {name: f.get("description") for name, f in obj["/light/action/turn_on"].fields.items()}
```

#### Finding Actions

The find methods go through actions along with everything else in the tree. An action's domain is `action` and its platform is the integration it belongs to, and `find_names()` gives it as Home Assistant names it, `mqtt.publish`.

```python
>>> list(obj.find_names(domain="action"))                  # every action
>>> list(obj.find_paths("/mqtt"))                          # mqtt's entities and its actions
>>> list(obj.find_paths("/mqtt/action"))                   # just its actions
>>> list(obj.find_paths("/.*/action/reload"))              # everything with a reload
>>> list(obj.find_names(domain="action", platform="mqtt"))
```

So a search gives both, unless it is kept to one kind with `object_type`:

```python
>>> list(obj.find(platform="mqtt", object_type="entity"))    # mqtt's entities, no actions
>>> list(obj.find_names("/mqtt", object_type="action"))      # mqtt's actions, no entities
```

`object_type` is `"entity"`, `"action"` or `"device"`, or a list of them, and anything else is a `ValueError`. Code that reads `state` or `entity_id` from each result wants `object_type="entity"`. Devices are not in the tree yet, so `"device"` finds nothing for now.

An action has no area or label, so a search by either finds none.
