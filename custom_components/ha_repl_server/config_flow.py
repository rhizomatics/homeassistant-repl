"""Config flow for Home Assistant REPL: a single entry, added from the UI
(gated behind explicit risk acknowledgment - see _ACK_FIELDS below, the same
"are you sure you know what you're doing" pattern HACS's own config flow
uses) or imported from YAML (`ha_repl_server:` in configuration.yaml, left
as a no-form, always-on path for backward compatibility with existing
installs that predate this gate).

What's exposed (`expose_hass`/`expose_sql`) is deliberately not on that
initial form: it's an ongoing setting, not a one-time risk acknowledgment,
so it lives in the entry's *options* - editable any time from Settings >
Devices & services > Home Assistant REPL > Configure - rather than data
fixed at creation.

Either can be given a time limit there, in minutes, after which it is
switched off again (see __init__.py's _expire). The entry keeps the time
that falls at, as `expose_<name>_until`, not the minutes, so the limit
survives a restart. With no limit entered, it stays on.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.util import dt as dt_util

from .const import CAPABILITIES, DOMAIN

TITLE = "Home Assistant REPL Live Server"

# Both default on - matches the always-on behaviour of every version before
# this option existed, so an entry that predates it (or was never
# reconfigured) keeps working the same way.
_DEFAULT_OPTIONS = {"expose_hass": True, "expose_sql": True}

# Every one of these must be truthy in user_input before the entry is
# created (see async_step_user) - each is its own field (rather than one
# "I agree" checkbox) so the form, and this module's own code, name the
# specific thing being acknowledged.
_ACK_FIELDS = (
    "ack_stability",
    "ack_sql_not_guaranteed",
    "ack_secrets",
    "ack_token",
)

_ACK_SCHEMA = vol.Schema({
    vol.Required(field, default=False): bool for field in _ACK_FIELDS
})


class HaReplServerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> HaReplServerOptionsFlow:
        return HaReplServerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if all(user_input.get(field) for field in _ACK_FIELDS):
                return self.async_create_entry(
                    title=TITLE, data={}, options=_DEFAULT_OPTIONS
                )
            errors["base"] = "ack_required"
        return self.async_show_form(
            step_id="user",
            data_schema=_ACK_SCHEMA,  # type: ignore[arg-type] #ty: ignore[invalid-argument-type]
            errors=errors,
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title=TITLE, data={}, options=_DEFAULT_OPTIONS)


def _minutes_left(options: Any, name: str, now: datetime) -> int | None:
    """Whole minutes, rounded up, until `expose_<name>` is switched off -
    None if it has no time limit, or is past it."""
    until = dt_util.parse_datetime(options.get(f"expose_{name}_until") or "")
    if until is None or until <= now:
        return None
    return math.ceil((until - now) / timedelta(minutes=1))


class HaReplServerOptionsFlow(OptionsFlowWithReload):
    """`expose_hass`/`expose_sql`, and a time limit for each - editable
    after setup; changing any reloads the entry automatically
    (OptionsFlowWithReload's own job) so async_setup_entry re-reads them
    immediately, no manual listener needed.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        now = dt_util.utcnow()
        if user_input is not None:
            data: dict[str, Any] = {}
            for name in CAPABILITIES:
                data[f"expose_{name}"] = user_input[f"expose_{name}"]
                minutes = user_input.get(f"expose_{name}_minutes")
                if not data[f"expose_{name}"] or minutes is None:
                    continue  # off, or on with no limit: nothing to run out
                if minutes == _minutes_left(options, name, now):
                    # the limit already running, as the form showed it
                    data[f"expose_{name}_until"] = options[f"expose_{name}_until"]
                else:
                    until = now + timedelta(minutes=minutes)
                    data[f"expose_{name}_until"] = until.isoformat()
            return self.async_create_entry(title="", data=data)
        fields: dict[Any, Any] = {}
        for name in CAPABILITIES:
            exposed = options.get(f"expose_{name}", True)
            fields[vol.Required(f"expose_{name}", default=exposed)] = bool
            # A suggested value, not a default: clearing the field has to
            # be a way of saying "no limit".
            left = _minutes_left(options, name, now) if exposed else None
            fields[
                vol.Optional(
                    f"expose_{name}_minutes", description={"suggested_value": left}
                )
            ] = vol.All(vol.Coerce(int), vol.Range(min=1))
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]
