"""Diagnostics for the Live Server: how it's configured, what loaded, and the
sessions it holds - by name and variable name only, never a variable's value,
which could be anything a session has computed or fetched."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .session import HaReplConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaReplConfigEntry
) -> dict[str, Any]:
    manager = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    return {
        "version": integration.version,
        # Switches, time limits and completion limits: no secrets among them
        "options": dict(entry.options),
        "sql": {
            "available": manager.has_feature("sql"),
            "unavailable_because": manager.unavailable.get("sql"),
        },
        "complete": {
            "limit": manager.complete_limit,
            "budget_seconds": manager.complete_budget,
        },
        "sessions": manager.describe(),
    }
