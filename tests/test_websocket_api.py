"""Tests for the ha_repl_server/* websocket commands - via the test
harness's real `hass_ws_client`, so this exercises actual command
registration/schema validation, not just the handler functions directly."""

from __future__ import annotations

import asyncio
import base64
import logging
import re

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_repl_server.const import DOMAIN
from homeassistant_repl.sql import SqlResult as ClientSqlResult


async def test_ws_info_returns_manifest_version(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None, hass_ws_client
):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    integration = await async_get_integration(hass, DOMAIN)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "ha_repl_server/info"})
    response = await client.receive_json()

    assert response["success"]
    assert response["result"] == {"version": integration.version}


async def _setup(hass: HomeAssistant, **options) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_ws_exec_show_looks_inside_a_home_assistant_object(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None, hass_ws_client
):
    await _setup(hass)
    hass.states.async_set("sensor.test", "42", {"unit_of_measurement": "W"})
    client = await hass_ws_client(hass)

    ids = iter(range(1, 10))

    async def show(code: str) -> dict:
        await client.send_json({
            "id": next(ids),
            "type": "ha_repl_server/exec",
            "code": code,
        })
        result = (await client.receive_json())["result"]
        assert result["error"] is None
        assert result["value_tree"]["t"] == "obj"
        return {"": result["value_tree"]["n"], **dict(result["value_tree"]["f"])}

    state = await show("show(hass.states.get('sensor.test'))")
    assert state[""] == "State"
    assert (state["entity_id"], state["state"]) == ("sensor.test", "42")
    assert state["attributes"] == {"t": "dict", "v": [["unit_of_measurement", "W"]]}
    assert not any(name.startswith("_") for name in state)

    # the biggest object there is comes back, and comes back small
    root = await show("show(hass)")
    assert root[""] == "HomeAssistant"
    assert {"config", "states", "data"} <= set(root)
    assert len(str(root)) < 100_000


async def test_ws_sql_returns_arrow_the_client_can_read(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None, hass_ws_client
):
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({
        "id": 1,
        "type": "ha_repl_server/sql",
        "query": "select 1 as a, 'x' as b union all select 2, 'y'",
        "max_rows": 1,
    })
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["truncated"] is True
    result = ClientSqlResult.from_arrow(base64.b64decode(response["result"]["arrow"]))
    assert result.to_dicts() == [{"a": 1, "b": "x"}]


async def test_ws_sql_reports_a_rejected_query_as_sql_error(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None, hass_ws_client
):
    await _setup(hass)
    client = await hass_ws_client(hass)

    for msg_id, query in enumerate(("delete from states", "select * from nope"), 1):
        await client.send_json({
            "id": msg_id,
            "type": "ha_repl_server/sql",
            "query": query,
        })
        response = await client.receive_json()
        assert not response["success"]
        assert response["error"]["code"] == "sql_error"


async def test_ws_sql_tables_lists_recorder_tables_and_columns(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None, hass_ws_client
):
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "ha_repl_server/sql_tables"})
    response = await client.receive_json()

    tables = {t["name"]: t["columns"] for t in response["result"]["tables"]}
    assert "states" in tables
    assert {
        "name": "entity_id",
        "type": "VARCHAR(255)",
        "legacy": False,
    } in tables["states_meta"]


async def test_ws_sql_commands_honour_expose_sql_off(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None, hass_ws_client
):
    await _setup(hass, expose_sql=False)
    client = await hass_ws_client(hass)

    for msg_id, msg in enumerate(
        (
            {"type": "ha_repl_server/sql", "query": "select 1"},
            {"type": "ha_repl_server/sql_tables"},
        ),
        1,
    ):
        await client.send_json({"id": msg_id, **msg})
        response = await client.receive_json()
        assert not response["success"]
        assert response["error"]["code"] == "sql_disabled"


async def test_ws_cancel_stops_a_running_exec_and_both_are_logged(
    recorder_mock,
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_ws_client,
    caplog,
):
    caplog.set_level(logging.INFO, logger="custom_components.ha_repl_server")
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({
        "id": 1,
        "type": "ha_repl_server/exec",
        "code": "import asyncio\nawait asyncio.sleep(30)",
    })
    await asyncio.sleep(0.05)
    await client.send_json({
        "id": 2,
        "type": "ha_repl_server/cancel",
        "session": "default",
    })
    replies = {}
    while len(replies) < 2:
        reply = await client.receive_json()
        replies[reply["id"]] = reply
    assert replies[2]["result"] == {"stopped": ["default"]}
    error = replies[1]["result"]["error"]
    assert error["type"] == "CancelledError"
    assert error["message"].startswith("Stopped by ")

    logged = caplog.text
    assert (
        "runs in session 'default':\nimport asyncio\nawait asyncio.sleep(30)" in logged
    )
    assert "run in session 'default' raised CancelledError: Stopped by" in logged
    assert "stopped session 'default'" in logged


async def test_ws_exec_logs_the_outcome_with_its_timing(
    recorder_mock,
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_ws_client,
    caplog,
):
    caplog.set_level(logging.INFO, logger="custom_components.ha_repl_server")
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "ha_repl_server/exec", "code": "6 * 7"})
    assert (await client.receive_json())["success"]
    assert re.search(
        r"run in session 'default' returned 42 in \d+\.\d{3}s", caplog.text
    )


async def test_ws_sql_logs_the_query_and_its_rows(
    recorder_mock,
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_ws_client,
    caplog,
):
    caplog.set_level(logging.INFO, logger="custom_components.ha_repl_server")
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({
        "id": 1,
        "type": "ha_repl_server/sql",
        "query": "select 1 as n",
    })
    assert (await client.receive_json())["success"]
    assert "queries:\nselect 1 as n" in caplog.text
    assert re.search(r"query returned 1 rows in \d+\.\d{3}s", caplog.text)


async def test_stop_button_stops_every_session(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None, hass_ws_client
):
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({
        "id": 1,
        "type": "ha_repl_server/exec",
        "code": "import asyncio\nawait asyncio.sleep(30)",
    })
    await asyncio.sleep(0.05)
    (entity_id,) = hass.states.async_entity_ids("button")
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    error = (await client.receive_json())["result"]["error"]
    assert error["type"] == "CancelledError"
    assert error["message"].endswith("from the Stop button")
