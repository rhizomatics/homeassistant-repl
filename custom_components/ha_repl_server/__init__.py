"""Home Assistant REPL: a live Python REPL inside Home Assistant, served over the websocket API.

Set up from Settings > Devices & services > Add integration, or with `ha_repl_server:` in
configuration.yaml (imported as a config entry). Executes arbitrary code as admin; never
enable it on an instance where admin accounts are not fully trusted.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import websocket_api
from .const import (
    CAPABILITIES,
    DEFAULT_COMPLETE_BUDGET_MS,
    DEFAULT_COMPLETE_LIMIT,
    DOMAIN,
)
from .objtree import ObjTree
from .session import SessionManager

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

# The Stop button, see button.py.
PLATFORMS = [Platform.BUTTON]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # Commands stay registered for the life of the process (HA has no unregister);
    # they report an error while no config entry is loaded.
    websocket_api.async_register(hass)
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data={}
            )
        )
    return True


def _switch_off(hass: HomeAssistant, entry: ConfigEntry, names: list[str]) -> None:
    """Record in the entry's options that the time limit on each of these
    has run out: switched off, as if from Configure, with no limit left."""
    options = dict(entry.options)
    for name in names:
        options[f"expose_{name}"] = False
        options.pop(f"expose_{name}_until", None)
        _LOGGER.warning("Home Assistant REPL: time limit on `%s` reached", name)
    hass.config_entries.async_update_entry(entry, options=options)


def _apply_time_limits(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Switch off what ran out of time while this wasn't running, and
    arrange for the rest to be when their time comes."""
    now = dt_util.utcnow()
    limits = {
        name: until
        for name in CAPABILITIES
        if entry.options.get(f"expose_{name}", True)
        and (
            until := dt_util.parse_datetime(
                entry.options.get(f"expose_{name}_until") or ""
            )
        )
    }
    if expired := [name for name, until in limits.items() if until <= now]:
        _switch_off(hass, entry, expired)

    def _expire(name: str) -> Any:
        @callback
        def expire(_now: datetime) -> None:
            _switch_off(hass, entry, [name])
            # a reload is how any change of options takes effect
            hass.config_entries.async_schedule_reload(entry.entry_id)

        return expire

    for name, until in limits.items():
        if name not in expired:
            entry.async_on_unload(
                async_track_point_in_utc_time(hass, _expire(name), until)
            )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _apply_time_limits(hass, entry)
    # In entry.options, not entry.data: editable after setup via Configure
    # (see config_flow.py's HaReplServerOptionsFlow), not fixed at creation.
    # Missing (not just False) means an entry from before these existed -
    # both default on, matching that version's always-on behaviour.
    bindings: dict[str, Any] = {"obj": ObjTree(hass)}
    if entry.options.get("expose_hass", True):
        bindings["hass"] = hass
    # sql isn't a session binding: the client's own `sql` object fetches
    # Arrow data through ha_repl_server/sql, which checks this feature.
    features = {"sql"} if entry.options.get("expose_sql", True) else set()

    hass.data[DOMAIN] = SessionManager(
        bindings,
        frozenset(features),
        complete_limit=entry.options.get("complete_limit", DEFAULT_COMPLETE_LIMIT),
        complete_budget=entry.options.get(
            "complete_budget_ms", DEFAULT_COMPLETE_BUDGET_MS
        )
        / 1000,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.warning(
        "Home Assistant REPL is enabled: admin users can execute arbitrary Python in this instance"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if manager := hass.data.pop(DOMAIN, None):
        manager.cancel(None, "Stopped: Home Assistant REPL unloaded")
    return True
