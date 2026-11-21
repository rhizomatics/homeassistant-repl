"""What Home Assistant has to offer inside a string, for complete.py's
`strings`: entity ids, action domains and names, and config entry ids,
chosen by the name the string is given to - a parameter, keyword or dict
key - so most of Home Assistant's own API, and most custom components',
complete without a list of methods to keep up to date. See RFC 0002.

Only reads what's already in memory: the state machine, the action
registry and the config entries. Called on the event loop, where they're
meant to be read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from homeassistant.core import HomeAssistant

from .complete import Item, Strings

ENTITY_IDS = frozenset({"entity_id", "entity_ids"})
DOMAINS = frozenset({"domain"})
ACTIONS = frozenset({"service", "action"})
ENTRY_IDS = frozenset({"entry_id", "config_entry_id"})


def ha_strings(hass: HomeAssistant) -> Strings:
    def strings(name: str, given: Mapping[str, str]) -> Iterable[Item] | None:
        if name in ENTITY_IDS:
            return [
                Item(state.entity_id, "entity", state.name)
                for state in hass.states.async_all()
            ]
        if name in DOMAINS:
            return [
                Item(domain, "domain", f"{len(actions)} actions")
                for domain, actions in hass.services.async_services().items()
            ]
        if name in ACTIONS and (domain := given.get("domain")):
            return [
                Item(action, "action", domain)
                for action in hass.services.async_services_for_domain(domain)
            ]
        if name in ENTRY_IDS:
            return [
                Item(entry.entry_id, "config entry", f"{entry.domain}: {entry.title}")
                for entry in hass.config_entries.async_entries()
            ]
        return None

    return strings
