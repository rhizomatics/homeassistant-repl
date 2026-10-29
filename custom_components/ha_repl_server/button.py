"""A Stop button, on the integration's own device, stopping the code running
in every live session - from Home Assistant itself, a dashboard or an
automation, when the shell that started it can't (gone, or stuck waiting)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .session import SessionManager
from .websocket_api import COMMAND_LOG


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([StopButton(entry)])


class StopButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "stop"

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_stop"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        manager: SessionManager | None = self.hass.data.get(DOMAIN)
        if manager is None:
            return
        who = "Home Assistant"
        if self._context and self._context.user_id:
            user = await self.hass.auth.async_get_user(self._context.user_id)
            who = user.name or user.id if user else who
        stopped = manager.cancel(None, f"Stopped by {who}, from the Stop button")
        COMMAND_LOG.warning(
            "%s stopped %s from the Stop button",
            who,
            ", ".join(f"session {name!r}" for name in stopped)
            or "nothing (none running)",
        )
