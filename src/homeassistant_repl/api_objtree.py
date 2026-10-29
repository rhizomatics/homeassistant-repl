"""ApiObjTree - API client mode's `obj[...]`, built entirely from
Home Assistant's standard websocket API (get_states, config/*_registry/list)
- no ha_repl_server component needed, works against any instance an admin
token can reach. Bound to the same name, `obj`, as the live tree (see
apirepl.py) so a snippet that only touches `obj` runs unchanged in either
mode. Entities only have what that API exposes: state + attributes from
get_states, and whatever the entity registry's own partial dict exposes (no
live component instance, so no calling methods on it, no fields an
integration keeps off the registry/state).

Actions (services) are in the tree too, from get_services, under the
integration that registers them: `obj["/mqtt/action/publish"]`. Looking one
up reads the snapshot like anything else here; calling it is the one thing
in this tree that isn't read-only, and goes to Home Assistant as a
call_service.

Mirrors custom_components/ha_repl_server/objtree.py's shape - __getitem__,
.keys()/.values()/.items(), find(), show() all behave the same way modulo the
smaller attribute set - but lives in this package rather than that one (a
HACS-deployed component with its own packaging boundary), so the handful of
small, stable, HA-import-free pieces it needs (the ordered-view mixin,
parse_path, the None/empty-collection cleanup) are duplicated here rather than
imported across that boundary. Revisit if API client mode sticks and a shared
package becomes worth the packaging work.

Unlike the live tree, this one is explicitly a *snapshot*: fetching is async
(it's a websocket call) but Mapping's `__getitem__`/`__iter__`/`__len__` are
not, so refreshing can't happen lazily on access without risking asyncio
reentrancy (this is normally used from inside an already-running event loop).
Instead the owning REPL loop decides when to call `await cache.refresh()`
(e.g. between prompts, when `cache.is_stale()`) - see apirepl.py. `reset_cache()`
just marks the cache stale; it does not fetch.
"""

from __future__ import annotations

import re
import time
from collections.abc import (
    ItemsView,
    Iterable,
    Iterator,
    KeysView,
    Mapping,
    Sequence,
    ValuesView,
)
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple, Protocol, Self


class _ApiClient(Protocol):
    """What Cache actually needs from a client - just enough to let tests use
    a lightweight fake instead of a real websocket Client."""

    async def call(self, type_: str, **payload: Any) -> Any: ...


def parse_path(path: str) -> tuple[str, ...]:
    """Split "/integration[/domain[/object_id]]" into 1-3 parts. Duplicated
    from custom_components/ha_repl_server/paths.py - see the module
    docstring for why."""
    parts = tuple(p for p in path.split("/") if p)
    if not parts or len(parts) > 3:
        raise ValueError(f"{path!r} - expected /integration/domain/object_id")
    return parts


def _as_set(value: str | list[str] | None) -> set[str]:
    if value is None:
        return set()
    return {value} if isinstance(value, str) else set(value)


# HA's own slugs (integration/domain/object_id) are always [a-z0-9_] - never any
# of these - so a `find()` path containing one is unambiguously a regex, not a
# literal path, with no risk of misreading a real path as a pattern.
_REGEX_METACHARS = frozenset(".^$*+?{}[]|()\\")


def _require_str(value: Any, what: str = "path") -> str:
    """A clear TypeError at the boundary beats a confusing one from deep
    inside parse_path - duplicated from objtree.py's identical helper, see
    that module for the rationale."""
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str, not {type(value).__name__}")
    return value


class _OrderedView(Iterable[Any]):
    """Positional access/slicing on top of a MappingView's existing order -
    duplicated from objtree.py's identical mixin, see that module for the
    rationale."""

    def __getitem__(self, index: int | slice) -> Any:
        return tuple(self)[index]

    def __reversed__(self) -> Iterator[Any]:
        return reversed(tuple(self))


class OrderedKeysView(_OrderedView, KeysView, Sequence):
    pass


class OrderedValuesView(_OrderedView, ValuesView, Sequence):
    pass


class OrderedItemsView(_OrderedView, ItemsView, Sequence):  # type: ignore[misc]  # ty: ignore[invalid-method-override]
    # Sequence.__contains__(value) vs ItemsView.__contains__(item: tuple) is a
    # real static Liskov mismatch, but fine at runtime - both just delegate to
    # tuple(self).__contains__ via _OrderedView/Iterable, deliberately getting
    # both Set and Sequence behavior at once (see the module docstring).
    pass


class ApiPlatform(str):  # noqa: FURB189 - has to be a real str: compared, printed, sent as JSON
    """The integration an entity belongs to. A plain string - "mqtt" - that
    can also be read the way a live entity's `platform` is, as
    `platform.platform_name` and `platform.domain`, so the same expression
    works on an ApiEntity and on a real Entity."""

    __slots__ = ("domain",)

    domain: str

    def __new__(cls, platform_name: str, domain: str) -> Self:
        self = super().__new__(cls, platform_name)
        self.domain = domain
        return self

    # So it can be copied and pickled - one more argument than str's own.
    def __getnewargs__(self) -> tuple[str, str]:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        return (str(self), self.domain)

    @property
    def platform_name(self) -> str:
        return str(self)


class ApiRegistryEntry(dict[str, Any]):  # noqa: FURB189 - has to be a real dict, as above
    """An entity's registry entry. A dict, that can also be read the way a
    live entity's `registry_entry` is - `registry_entry.unique_id` - so the
    same expression works on an ApiEntity and on a real Entity."""

    # Its attributes, keys and dir() only look things up: completion may use
    # them (complete.py).
    _ha_repl_inspectable = True

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __dir__(self) -> list[str]:
        """Its keys too, so they complete as attributes."""
        keys = {k for k in self if isinstance(k, str) and k.isidentifier()}
        return sorted(set(super().__dir__()) | keys)


@dataclass(frozen=True)
class ApiEntity:
    """One entity's worth of API client mode data - only what get_states and
    the entity registry's partial dict expose, not the live component instance.

    Named to match the real Entity's own attributes where there's a direct
    equivalent - state, state_attributes, name, platform, registry_entry and
    the properties below - per
    https://developers.home-assistant.io/docs/core/entity/, so code written
    against one reads the same against the other. `domain`, `object_id`,
    `area_id` and `labels` are extras a real Entity doesn't have. `name` comes from
    get_states' `friendly_name` attribute: that's already HA's own fully
    resolved result of computing Entity.name (registry override, device name,
    has_entity_name, etc.), published into the state machine for exactly this
    kind of external consumer - cheaper and more faithful than recomputing it
    from the pieces ourselves. `state_attributes` is everything else in that
    attributes dict; broader than the live property of the same name (which
    excludes things like unit_of_measurement that come from separate Entity
    properties), since the state machine doesn't preserve that distinction.
    """

    entity_id: str
    platform: ApiPlatform
    domain: str
    object_id: str
    state: str | None
    name: str | None
    state_attributes: dict[str, Any]
    area_id: str | None
    labels: frozenset[str]
    registry_entry: ApiRegistryEntry

    # The real Entity's own properties, as far as the state and registry
    # entry can answer them - same names, so they read the same in either mode.

    @property
    def unique_id(self) -> str | None:
        return self.registry_entry.get("unique_id")

    @property
    def available(self) -> bool:
        return self.state not in (None, "unavailable")

    @property
    def enabled(self) -> bool:
        return self.registry_entry.get("disabled_by") is None

    @property
    def device_class(self) -> str | None:
        return self.state_attributes.get("device_class")

    @property
    def unit_of_measurement(self) -> str | None:
        return self.state_attributes.get("unit_of_measurement")

    @property
    def icon(self) -> str | None:
        return self.state_attributes.get("icon")

    @property
    def entity_picture(self) -> str | None:
        return self.state_attributes.get("entity_picture")

    @property
    def supported_features(self) -> int | None:
        return self.state_attributes.get("supported_features")

    @property
    def assumed_state(self) -> bool:
        return bool(self.state_attributes.get("assumed_state", False))

    @property
    def attribution(self) -> str | None:
        return self.state_attributes.get("attribution")

    @property
    def entity_category(self) -> str | None:
        return self.registry_entry.get("entity_category")

    @property
    def has_entity_name(self) -> bool:
        return bool(self.registry_entry.get("has_entity_name", False))

    @property
    def translation_key(self) -> str | None:
        return self.registry_entry.get("translation_key")


# The level of the tree an integration's actions are under, beside its
# entity domains - `/mqtt/action/publish`. Not an entity domain itself.
ACTION = "action"


def _describe_action(
    domain: str, service: str, described: dict[str, Any], translations: dict[str, str]
) -> dict[str, Any]:
    """One action's description as a person would want it: what Home
    Assistant keeps about it (its fields, from services.yaml) with the
    names and descriptions the frontend shows, which are kept apart from
    that, in the translations. Fields in a collapsible section of the
    form are listed with the rest.

    Duplicated from custom_components/ha_repl_server/objtree.py.
    """
    base = f"component.{domain}.services.{service}"
    fields: dict[str, Any] = {}

    def add(specs: Any) -> None:
        for key, spec in (specs or {}).items():
            spec = dict(spec or {})
            if "fields" in spec:  # a section, not a field
                add(spec["fields"])
                continue
            texts = {
                item: translations.get(f"{base}.fields.{key}.{item}")
                for item in ("name", "description")
                if item not in spec
            }
            fields[key] = {**{k: v for k, v in texts.items() if v}, **spec}

    add(described.get("fields"))
    result: dict[str, Any] = {
        "name": described.get("name") or translations.get(f"{base}.name"),
        "description": described.get("description")
        or translations.get(f"{base}.description"),
        "fields": fields,
    }
    if "target" in described:
        result["target"] = described["target"]
    return result


@dataclass(frozen=True, repr=False)
class ApiAction:
    """One action (service) of an integration, as `obj["/mqtt/action/publish"]`
    gives it. Call it to run the action, with the action's data as keyword
    arguments:

        await obj["/mqtt/action/publish"](topic="shed/fan", payload="on")
        await obj["/light/action/turn_on"](target={"entity_id": "light.shed"})
        await obj["/weather/action/get_forecasts"](entity_id="weather.home", type="daily")

    What an action that answers gives back is returned. `name`,
    `description` and `fields` are Home Assistant's description of it.
    """

    domain: str
    service: str
    name: str | None
    description: str | None
    fields: dict[str, Any]
    """Each field the action takes: its name and description, whether it
    is required, an example, and the selector the frontend uses."""
    supports_response: str
    """"none", "optional" or "only" - whether calling it gives data back."""
    _client: _ApiClient = field(compare=False)

    @property
    def action_id(self) -> str:
        """The action as Home Assistant names it, e.g. `mqtt.publish`."""
        return f"{self.domain}.{self.service}"

    async def describe(self) -> dict[str, Any]:
        """Home Assistant's description of the action - its `name`,
        `description` and `fields` - as one dict. Something to await, as
        it is in live mode."""
        return {
            "name": self.name,
            "description": self.description,
            "fields": self.fields,
        }

    async def __call__(
        self,
        *,
        target: dict[str, Any] | None = None,
        return_response: bool | None = None,
        **data: Any,
    ) -> Any:
        """Run the action and wait for it, with `data` as its data and
        `target` as what it is to act on (`{"entity_id": ...}`,
        `{"area_id": ...}`). Returns what it answers with, if it is one that
        answers - `return_response` says whether to ask, for one where
        that is optional."""
        if return_response is None:
            return_response = self.supports_response == "only"
        payload: dict[str, Any] = {
            "domain": self.domain,
            "service": self.service,
            "service_data": data,
            "return_response": return_response,
        }
        if target is not None:
            payload["target"] = target
        result = await self._client.call("call_service", **payload)
        return (result or {}).get("response") if return_response else None

    def __repr__(self) -> str:
        return f"<action {self.action_id}>"


def _to_action(
    client: _ApiClient,
    domain: str,
    service: str,
    described: dict[str, Any],
    translations: dict[str, str],
) -> ApiAction:
    """An ApiAction from one service's entry in a get_services reply, where
    `response` is there only for one that answers."""
    response = described.get("response")
    supports = (
        "none"
        if response is None
        else "optional"
        if response.get("optional")
        else "only"
    )
    full = _describe_action(domain, service, described, translations)
    return ApiAction(
        domain=domain,
        service=service,
        name=full["name"],
        description=full["description"],
        fields=full["fields"],
        supports_response=supports,
        _client=client,
    )


def _name_of(thing: Any) -> str:
    """What find_names() gives for something found: an entity's id, or an
    action's - `light.shed`, `mqtt.publish`."""
    return thing.action_id if isinstance(thing, ApiAction) else thing.entity_id


# What find() can be told to look for. Devices are not in the tree yet, so
# asking for them alone finds nothing.
OBJECT_TYPES = ("entity", "action", "device")


def _object_types(value: str | list[str] | None) -> set[str]:
    """The kinds of object a find is for - all of them if none is named."""
    types = _as_set(value)
    if unknown := types - set(OBJECT_TYPES):
        raise ValueError(
            f"object_type must be one of {', '.join(OBJECT_TYPES)}, "
            f"not {', '.join(sorted(unknown))}"
        )
    return types or set(OBJECT_TYPES)


class _Found(NamedTuple):
    """Internal only - never returned from find()/find_paths()/find_names(),
    just the shared (path, entity) pair each of them projects from
    (.entity, .path, and .entity_id respectively) so the filtering logic in
    _find() lives in exactly one place. Any attribute other than path/entity
    falls through to `entity`, so e.g. found.entity_id works without
    found.entity.entity_id."""

    path: str
    entity: ApiEntity | ApiAction

    def __getattr__(self, name: str) -> Any:
        return getattr(self.entity, name)


# The two registry JSON fields that are Unix-timestamp floats rather than the
# ISO datetime strings get_states already returns - see show()/_clean().
_TIMESTAMP_FIELDS = frozenset({"created_at", "modified_at"})


class Cache:
    """Fetches and holds one snapshot of states + registries. Refresh is
    driven by the REPL loop between commands - see the module docstring."""

    def __init__(self, client: _ApiClient, ttl: float) -> None:
        self.client = client
        self.ttl = ttl
        self._fetched_at: float | None = None
        self.entities: dict[str, ApiEntity] = {}
        self.actions: dict[tuple[str, str], ApiAction] = {}
        self.areas: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, dict[str, Any]] = {}

    def is_stale(self) -> bool:
        return (
            self._fetched_at is None or (time.monotonic() - self._fetched_at) > self.ttl
        )

    def reset(self) -> None:
        """Mark the cache stale. Doesn't fetch - see the module docstring."""
        self._fetched_at = None

    async def refresh(self) -> None:
        states = {s["entity_id"]: s for s in await self.client.call("get_states")}
        registry_entries = await self.client.call("config/entity_registry/list")
        devices = {
            d["id"]: d for d in await self.client.call("config/device_registry/list")
        }
        self.areas = {
            a["area_id"]: a for a in await self.client.call("config/area_registry/list")
        }
        self.labels = {
            l["label_id"]: l
            for l in await self.client.call("config/label_registry/list")
        }

        entities: dict[str, ApiEntity] = {}
        for entry in registry_entries:
            entity_id = entry["entity_id"]
            domain, object_id = entity_id.split(".", 1)
            state = states.get(entity_id, {})
            area_id = entry.get("area_id")
            if area_id is None and entry.get("device_id"):
                device = devices.get(entry["device_id"])
                if device is not None:
                    area_id = device.get("area_id")
            # friendly_name is already HA's own fully-resolved Entity.name,
            # published for exactly this kind of external consumer - see
            # ApiEntity's docstring. Copy the dict before popping: it's
            # `states[entity_id]`'s own attributes dict, not ours to mutate.
            attributes = dict(state.get("attributes", {}))
            name = attributes.pop("friendly_name", None)
            entities[entity_id] = ApiEntity(
                entity_id=entity_id,
                platform=ApiPlatform(entry["platform"], domain),
                domain=domain,
                object_id=object_id,
                state=state.get("state"),
                name=name,
                state_attributes=attributes,
                area_id=area_id,
                labels=frozenset(entry.get("labels", ())),
                registry_entry=ApiRegistryEntry(entry),
            )
        self.entities = entities
        services = await self.client.call("get_services")
        try:
            # The names and descriptions the frontend shows for an action
            # and its fields - kept apart from the rest, as translations.
            config = await self.client.call("get_config")
            reply = await self.client.call(
                "frontend/get_translations",
                language=config.get("language", "en"),
                category="services",
            )
            translations = reply.get("resources", {})
        except Exception:  # noqa: BLE001 - actions are just less well described without
            translations = {}
        self.actions = {
            (domain, service): _to_action(
                self.client, domain, service, described, translations
            )
            for domain, described_services in services.items()
            for service, described in described_services.items()
        }
        self._fetched_at = time.monotonic()


def _resolve_from_cache(
    values: str | list[str] | None,
    by_id: dict[str, dict[str, Any]],
    kind: str,
    id_key: str,
) -> set[str]:
    """Resolve each of `values` (an id or a display name) to its canonical id.

    Raises KeyError on anything that matches neither - a typo'd area/label
    name should fail loudly, not just quietly match nothing in find().
    """
    ids: set[str] = set()
    for value in _as_set(values):
        if value in by_id:
            ids.add(value)
            continue
        match = next(
            (
                k
                for k, v in by_id.items()
                if v.get("name", "").casefold() == value.casefold()
            ),
            None,
        )
        if match is None:
            raise KeyError(f"no such {kind}: {value!r}")
        ids.add(match)
    return ids


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return _clean(value)
    return value


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively drop None/empty-collection values and render the known
    timestamp fields as local ISO 8601 strings.

    Empty *lists* are dropped here too, not just empty sets: labels/aliases
    etc. arrive over the wire as JSON lists (JSON has no set type), so a list
    is this mode's wire-equivalent of the live tree's empty-set fields - and
    keeping both modes behaving the same way is the point.
    """
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if key in _TIMESTAMP_FIELDS and isinstance(value, (int, float)):
            value = datetime.fromtimestamp(value).astimezone().isoformat()
        else:
            value = _normalize(value)
        if value is None or value == set() or value == []:
            continue
        cleaned[key] = value
    return cleaned


@dataclass(frozen=True)
class ApiObjTree(Mapping[str, "ApiEntity | ApiAction | ApiObjTree"]):
    """The tree of Home Assistant's entities and actions, as read through its API.

    Entities are arranged by integration, then domain, then name:

        obj["/mqtt"]                    everything from one integration
        obj["/mqtt/sensor"]             its sensors
        obj["/mqtt/sensor/shed_temp"]   one entity
        obj["sensor.shed_temp"]         the same entity, by entity id

    A missing path or entity raises KeyError. Each level is a dict of the
    level below, in alphabetical order: len(), `in`, keys(), values() and
    items() all work on it, and those three can be indexed like a list.

    find() goes through every entity below a path instead, in no fixed
    order. It takes a path or regular expression, and any of domain=,
    platform=, area=, label= and object_type=, each a string or a list
    of strings.
    find_paths() and find_names() take the same and give tree paths or
    entity ids rather than the entities.

    show(path) gives the most useful details of one entity.

    An integration's actions (services) are beside its domains:

        obj["/mqtt/action"]             its actions
        obj["/mqtt/action/publish"]     one action - call it to run it

    The find methods go through actions as well as entities. Give them
    object_type="entity" or object_type="action" for just the one kind.

    This is a snapshot, refreshed between prompts once it is a while old.
    reset_cache() makes the next prompt fetch a new one.
    """

    # API client mode's view of the object tree - see the module docstring.

    # Its attributes, keys and dir() only look things up: completion may use
    # them (complete.py).
    _ha_repl_inspectable = True

    cache: Cache
    integration: str | None = None
    domain: str | None = None

    def reset_cache(self) -> None:
        self.cache.reset()

    def __getitem__(self, key: str) -> ApiEntity | ApiAction | ApiObjTree:
        _require_str(key, "key")
        if (
            "/" not in key
            and "." in key
            and self.integration is not None
            and self.domain is None
        ):
            # Courtesy: a full entity_id ("domain.object_id") also works
            # scoped to just an integration, not only at the root - translate
            # to the equivalent "/"-path so the logic below (which already
            # enforces the platform match) handles it the same way.
            domain, _, object_id = key.partition(".")
            key = f"{domain}/{object_id}"
        if "/" not in key and self.integration is None:
            entity = self.cache.entities.get(key)
            if entity is None:
                raise KeyError(key)
            return entity
        try:
            parts = parse_path(key)
        except ValueError as err:
            raise KeyError(str(err)) from None
        scope = tuple(p for p in (self.integration, self.domain) if p is not None)
        full = scope + parts
        if len(full) > 3:
            raise KeyError(key)
        if len(full) < 3:
            subtree = ApiObjTree(self.cache, *full)
            if not subtree:
                raise KeyError(key)
            return subtree
        integration, domain, object_id = full
        if domain == ACTION:
            action = self.cache.actions.get((integration, object_id))
            if action is None:
                raise KeyError(key)
            return action
        entity = self.cache.entities.get(f"{domain}.{object_id}")
        if entity is None or entity.platform != integration:
            raise KeyError(key)
        return entity

    def _actions(self, prefix: tuple[str, ...]) -> Iterator[ApiAction]:
        """Every action whose (integration, "action", name) matches `prefix`
        position by position, as _entries() does for entities."""
        if len(prefix) >= 2 and prefix[1] != ACTION:
            return
        for action in self.cache.actions.values():
            if len(prefix) >= 1 and action.domain != prefix[0]:
                continue
            if len(prefix) >= 3 and action.service != prefix[2]:
                continue
            yield action

    def _entries(self, prefix: tuple[str, ...]) -> Iterator[ApiEntity]:
        for entity in self.cache.entities.values():
            if len(prefix) >= 1 and entity.platform != prefix[0]:
                continue
            if len(prefix) >= 2 and entity.domain != prefix[1]:
                continue
            if len(prefix) >= 3 and entity.object_id != prefix[2]:
                continue
            yield entity

    def __iter__(self) -> Iterator[str]:
        scope = tuple(p for p in (self.integration, self.domain) if p is not None)
        children = {
            (entity.platform, entity.domain, entity.object_id)[len(scope)]
            for entity in self._entries(scope)
        }
        children |= {
            (action.domain, ACTION, action.service)[len(scope)]
            for action in self._actions(scope)
        }
        for child in sorted(children):
            yield "/" + child

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def _ipython_key_completions_(self) -> list[str]:
        """What `obj["` completes to in IPython, Jupyter and Marimo: every
        path below this level, and at the root every entity id as well."""
        scope = tuple(p for p in (self.integration, self.domain) if p is not None)
        below = [
            (entity.platform, entity.domain, entity.object_id)[len(scope) :]
            for entity in self._entries(scope)
        ] + [
            (action.domain, ACTION, action.service)[len(scope) :]
            for action in self._actions(scope)
        ]
        paths = {
            "/" + "/".join(parts[:n])
            for parts in below
            for n in range(1, len(parts) + 1)
        }
        if self.integration is None:
            paths |= set(self.cache.entities)
        return sorted(paths)

    def keys(self) -> OrderedKeysView:
        return OrderedKeysView(self)

    def values(self) -> OrderedValuesView:
        return OrderedValuesView(self)

    def items(self) -> OrderedItemsView:
        return OrderedItemsView(self)

    def _find(
        self,
        path: str,
        *,
        platform: str | list[str] | None,
        domain: str | list[str] | None,
        area: str | list[str] | None,
        label: str | list[str] | None,
        object_type: str | list[str] | None = None,
    ) -> Iterator[_Found]:
        """The real search, shared by find()/find_paths()/find_names() - each
        just projects a different field from the (path, entity) pairs this
        yields.

        `path` is normally an exact /integration/domain/object_id prefix, the
        same as indexing - but a path containing a regex metacharacter (e.g.
        "/mqtt/binary_sensor/barn.*") is matched as a regular expression
        against each candidate's full path instead, anchored at the start
        (so it behaves like a prefix match unless you anchor the end
        yourself with `$`). That trades the usual early narrowing for a scan
        of this view's whole subtree, filtered by the pattern.

        `domain` matches the HA domain (e.g. "light") across
        integrations, the same way `platform` does for the owning
        integration; `area`/`label` each take an id or a display name. Each
        of the four ORs within itself when given a list, and they AND
        together. No order guarantee; wrap in sorted(...) if you want one.

        Actions are found along with entities, each with "action" as its
        domain and the integration whose action it is as its platform. An
        action has no area or label, so a search by either finds none.

        `object_type` narrows it to "entity", "action" or "device", or a
        list of those - all of them when it isn't given.
        """
        _require_str(path)
        scope = tuple(p for p in (self.integration, self.domain) if p is not None)
        pattern = None
        if path not in ("", "/") and _REGEX_METACHARS.intersection(path):
            pattern = re.compile(path)
            prefix = scope
        else:
            try:
                prefix = scope if path in ("", "/") else scope + parse_path(path)
            except ValueError as err:
                raise KeyError(str(err)) from None
            if len(prefix) > 3:
                raise KeyError(path)

        platforms = _as_set(platform)
        domains = _as_set(domain)
        types = _object_types(object_type)
        area_ids = _resolve_from_cache(area, self.cache.areas, "area", "area_id")
        label_ids = _resolve_from_cache(label, self.cache.labels, "label", "label_id")

        def _matches() -> Iterator[_Found]:
            for entity in self._entries(prefix) if "entity" in types else ():
                if platforms and entity.platform not in platforms:
                    continue
                if domains and entity.domain not in domains:
                    continue
                if area_ids and entity.area_id not in area_ids:
                    continue
                if label_ids and not (entity.labels & label_ids):
                    continue
                full_path = "/" + "/".join(
                    (entity.platform, entity.domain, entity.object_id)[len(scope) :]
                )
                if pattern is not None and not pattern.match(full_path):
                    continue
                yield _Found(full_path, entity)
            if area_ids or label_ids or "action" not in types:
                return
            for action in self._actions(prefix):
                if platforms and action.domain not in platforms:
                    continue
                if domains and ACTION not in domains:
                    continue
                full_path = "/" + "/".join(
                    (action.domain, ACTION, action.service)[len(scope) :]
                )
                if pattern is not None and not pattern.match(full_path):
                    continue
                yield _Found(full_path, action)

        return _matches()

    def find(
        self,
        path: str = "/",
        *,
        platform: str | list[str] | None = None,
        domain: str | list[str] | None = None,
        area: str | list[str] | None = None,
        label: str | list[str] | None = None,
        object_type: str | list[str] | None = None,
        raw: bool = False,
    ) -> Iterator[ApiEntity | ApiAction] | Iterator[dict[str, Any]]:
        """Every entity (or raw dict, if raw=True) matching the filters under
        `path` (relative to this view, "/" meaning this view's whole
        subtree) - flat, skipping the directory-style one-level-at-a-time
        grouping .keys()/indexing give you. find_paths()/find_names() are the
        same search with just the tree-path or entity_id strings, if that's
        all you want - see _find() for the filters.

        `raw=True` yields the object as received instead of the typed,
        renamed-to-match-Entity ApiEntity view - exactly what show(path)
        would return for that path (the merged, cleaned registry+state
        dict). The dict's own "entity_id" key identifies which entity it
        came from.
        """
        matches = self._find(
            path,
            platform=platform,
            domain=domain,
            area=area,
            label=label,
            object_type=object_type,
        )
        if raw:
            return (self.show(found.path) for found in matches)
        return (found.entity for found in matches)

    def find_paths(
        self,
        path: str = "/",
        *,
        platform: str | list[str] | None = None,
        domain: str | list[str] | None = None,
        area: str | list[str] | None = None,
        label: str | list[str] | None = None,
        object_type: str | list[str] | None = None,
    ) -> Iterator[str]:
        """Just the tree-path strings from find() (e.g.
        "/demo/light/kitchen_lights") - see _find() for the filters."""
        return (
            found.path
            for found in self._find(
                path,
                platform=platform,
                domain=domain,
                area=area,
                label=label,
                object_type=object_type,
            )
        )

    def find_names(
        self,
        path: str = "/",
        *,
        platform: str | list[str] | None = None,
        domain: str | list[str] | None = None,
        area: str | list[str] | None = None,
        label: str | list[str] | None = None,
        object_type: str | list[str] | None = None,
    ) -> Iterator[str]:
        """Just the HA entity_id strings from find() (e.g.
        "light.kitchen_lights") - see _find() for the filters. Not the same
        as find_paths(): this is the flat entity_id, not this tree's
        /integration/domain/object_id path."""
        return (
            _name_of(found.entity)
            for found in self._find(
                path,
                platform=platform,
                domain=domain,
                area=area,
                label=label,
                object_type=object_type,
            )
        )

    def show(self, path: str) -> dict[str, Any]:
        """Same shape as the live tree's show() - the registry entry's own
        fields plus `state`/`state_attributes`, cleaned the same way (None/
        empty-collection dropped, timestamps as local ISO 8601)."""
        _require_str(path)
        scope = tuple(p for p in (self.integration, self.domain) if p is not None)
        try:
            full = scope + parse_path(path)
        except ValueError as err:
            raise KeyError(str(err)) from None
        if len(full) != 3:
            raise KeyError(f"{path!r} - show() needs a full entity path")
        integration, domain, object_id = full
        if domain == ACTION:
            action = self.cache.actions.get((integration, object_id))
            if action is None:
                raise KeyError(path)
            return _clean({
                "action": action.action_id,
                "name": action.name,
                "description": action.description,
                "fields": action.fields,
                "supports_response": action.supports_response,
            })
        entity = self.cache.entities.get(f"{domain}.{object_id}")
        if entity is None or entity.platform != integration:
            raise KeyError(path)

        data: dict[str, Any] = dict(entity.registry_entry)
        data["name"] = entity.name
        data["state"] = entity.state
        data["state_attributes"] = entity.state_attributes
        return _clean(data)

    def __repr__(self) -> str:
        # "(api)" is just a display hint for a human reading output - the
        # binding name and API are identical to the live tree's on purpose.
        scope = "/".join(p for p in (self.integration, self.domain) if p is not None)
        label = f"obj:/{scope}" if scope else "obj"
        kind = (
            "actions"
            if self.domain == ACTION
            else "entities"
            if self.domain is not None
            else "domains"
            if self.integration
            else "integrations"
        )
        return f"<{label}: {len(self)} {kind} (api)>"
