"""Tests for the config flow's risk-acknowledgment gate (see config_flow.py's
own docstring for why) and the separate options flow that holds
expose_hass/expose_sql - editable after setup, not part of that gate.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ha_repl_server.const import DOMAIN

_ALL_ACKS = {
    "ack_stability": True,
    "ack_sql_not_guaranteed": True,
    "ack_secrets": True,
    "ack_token": True,
}


async def test_user_flow_shows_a_form_first(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    # The feature toggles live in the options flow, not here.
    assert result["data_schema"] is not None
    assert "expose_hass" not in result["data_schema"].schema
    assert "expose_sql" not in result["data_schema"].schema


async def test_user_flow_rejects_submission_missing_an_acknowledgment(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    incomplete = {**_ALL_ACKS, "ack_token": False}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], incomplete
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "ack_required"}


async def test_user_flow_creates_entry_once_every_ack_is_checked(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _ALL_ACKS
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The acknowledgments themselves aren't persisted - they're a one-time
    # gate, not runtime state. Both toggles default on in entry.options.
    assert result["data"] == {}
    assert result["options"] == {"expose_hass": True, "expose_sql": True}


async def test_import_flow_defaults_both_toggles_on_with_no_form(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data={}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    assert result["options"] == {"expose_hass": True, "expose_sql": True}


async def test_options_flow_shows_current_values_as_defaults(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"expose_hass": False, "expose_sql": True}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["data_schema"] is not None
    defaults = {
        k.schema: k.default()
        for k in result["data_schema"].schema
        if not k.schema.endswith("_minutes")
    }
    assert defaults == {"expose_hass": False, "expose_sql": True}


async def test_options_flow_updates_entry_options_and_reloads(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN].has_feature("sql")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"expose_hass": True, "expose_sql": False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"expose_hass": True, "expose_sql": False}
    # OptionsFlowWithReload reloaded the entry - the session manager was
    # rebuilt with the new settings, not the old ones.
    assert not hass.data[DOMAIN].has_feature("sql")


async def _configure(hass: HomeAssistant, entry: MockConfigEntry, **user_input):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"expose_hass": True, "expose_sql": True, **user_input}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return form


async def test_a_time_limit_switches_just_that_capability_off_when_it_runs_out(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    await _configure(hass, entry, expose_sql_minutes=30)

    until = dt_util.parse_datetime(entry.options["expose_sql_until"])
    assert until is not None
    assert timedelta(minutes=29) < until - dt_util.utcnow() <= timedelta(minutes=30)
    assert "expose_hass_until" not in entry.options
    assert hass.data[DOMAIN].has_feature("sql")

    async_fire_time_changed(hass, until - timedelta(seconds=30))
    await hass.async_block_till_done()
    assert hass.data[DOMAIN].has_feature("sql")

    async_fire_time_changed(hass, until + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert not hass.data[DOMAIN].has_feature("sql")
    assert "hass" in hass.data[DOMAIN].get("default").globals_
    # as if it had been switched off from Configure
    assert entry.options == {"expose_hass": True, "expose_sql": False}


async def test_a_time_limit_that_ran_out_while_stopped_is_applied_at_setup(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    past = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"expose_hass": True, "expose_hass_until": past, "expose_sql": True},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert "hass" not in hass.data[DOMAIN].get("default").globals_
    assert hass.data[DOMAIN].has_feature("sql")
    assert entry.options == {"expose_hass": False, "expose_sql": True}


async def test_options_form_shows_a_running_limit_and_keeps_or_clears_it(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _configure(hass, entry, expose_hass_minutes=30)
    until = entry.options["expose_hass_until"]

    # sent back as the form showed it, the limit carries on, not restarts
    form = await _configure(hass, entry, expose_hass_minutes=30)
    suggested = {
        k.schema: k.description["suggested_value"]
        for k in form["data_schema"].schema
        if k.schema.endswith("_minutes")
    }
    assert suggested == {"expose_hass_minutes": 30, "expose_sql_minutes": None}
    assert entry.options["expose_hass_until"] == until

    # with no value entered, it stays on
    await _configure(hass, entry)
    assert entry.options == {"expose_hass": True, "expose_sql": True}

    # and a limit on something switched off is not kept
    await _configure(hass, entry, expose_sql=False, expose_sql_minutes=5)
    assert entry.options == {"expose_hass": True, "expose_sql": False}
