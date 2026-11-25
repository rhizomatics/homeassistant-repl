"""Smoke test for the custom component's real setup path - uses the real
`hass` fixture from pytest-homeassistant-custom-component instead of the
hand-rolled fakes test_session.py/test_api_objtree.py use, so it actually
exercises async_setup_entry/async_unload_entry rather than just the
HA-free pieces (session.py, api_objtree.py).

`recorder_mock` is listed first in each test's own parameters (not just
relied on via conftest.py) because it needs to prepare the database before
`hass` itself is built; `recorder` is now a declared manifest dependency
(for sql()), so config_entries.async_setup() needs a working one.
`enable_custom_integrations` has no such ordering requirement (it depends on
`hass` itself), so it's requested directly rather than via an autouse
fixture - see conftest.py for why that matters here specifically.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.requirements import RequirementsNotFound

from custom_components.ha_repl_server.const import DOMAIN
from custom_components.ha_repl_server.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ha_repl_server.objtree import ObjTree
from custom_components.ha_repl_server.session import SessionManager
from tests.ha_testing import MockConfigEntry


def _loaded(hass: HomeAssistant) -> SessionManager:
    """The loaded entry's SessionManager."""
    (entry,) = hass.config_entries.async_loaded_entries(DOMAIN)
    return entry.runtime_data


async def test_setup_and_unload_entry(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    manager = _loaded(hass)
    session = manager.get("default")
    assert session.globals_["hass"] is hass
    assert isinstance(session.globals_["obj"], ObjTree)
    # sql is served by its own websocket command, never bound into sessions.
    assert "sql" not in session.globals_
    assert manager.has_feature("sql")
    # Nor is hass_api: that's a REST client, which the shell has locally.
    assert "hass_api" not in session.globals_

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.config_entries.async_loaded_entries(DOMAIN)


async def test_setup_entry_respects_expose_hass_and_expose_sql_false(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"expose_hass": False, "expose_sql": False}
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    session = _loaded(hass).get("default")
    assert "hass" not in session.globals_
    assert not _loaded(hass).has_feature("sql")
    assert isinstance(session.globals_["obj"], ObjTree)

    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("target", "error"),
    [
        (
            "custom_components.ha_repl_server.async_process_requirements",
            RequirementsNotFound(DOMAIN, ["nanoarrow>=0.9.0"]),
        ),
        (
            "custom_components.ha_repl_server.import_module",
            ImportError("libnanoarrow.so: cannot open shared object file"),
        ),
    ],
)
async def test_sql_that_cannot_load_switches_off_only_sql(
    recorder_mock,
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_ws_client,
    caplog,
    target,
    error,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    with patch(target, side_effect=error):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    manager = _loaded(hass)
    assert not manager.has_feature("sql")
    assert manager.get("default").globals_["hass"] is hass
    assert "sql is unavailable, everything else works" in caplog.text

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "ha_repl_server/sql", "query": "select 1"})
    response = await client.receive_json()
    assert response["error"]["code"] == "sql_unavailable"
    assert str(error) in response["error"]["message"]
    await client.send_json({"id": 2, "type": "ha_repl_server/exec", "code": "1 + 1"})
    assert (await client.receive_json())["result"]["value"] == "2"

    # A repair issue says so, with the reason, until a reload loads it
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "sql_unavailable")
    assert issue is not None
    assert issue.translation_placeholders == {
        "reason": f"{type(error).__name__}: {error}"
    }
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _loaded(hass).has_feature("sql")
    assert ir.async_get(hass).async_get_issue(DOMAIN, "sql_unavailable") is None


async def test_diagnostics_say_how_it_is_set_up_and_never_a_value(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"expose_hass": True, "complete_limit": 200}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    _loaded(hass).get("default").globals_["token"] = "a value never to be shown"

    found = await async_get_config_entry_diagnostics(hass, entry)

    assert found["options"] == {"expose_hass": True, "complete_limit": 200}
    assert found["sql"] == {"available": True, "unavailable_because": None}
    assert found["complete"]["limit"] == 200
    (session,) = found["sessions"]
    assert session["name"] == "default"
    assert "token" in session["variables"]
    assert "a value never to be shown" not in repr(found)
    assert found["version"]
