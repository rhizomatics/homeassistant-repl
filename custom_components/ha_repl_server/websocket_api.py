"""Websocket commands: ha_repl_server/exec, ha_repl_server/reset,
ha_repl_server/sessions, ha_repl_server/info, ha_repl_server/sql and
ha_repl_server/sql_tables (all admin only)."""

from __future__ import annotations

import base64
import contextlib
from typing import Any, cast

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .const import DEFAULT_SESSION, DOMAIN
from .objtree import async_load_action_descriptions
from .session import SessionManager
from .sql import DEFAULT_MAX_ROWS, SqlError, sql, table_schemas


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_exec)
    websocket_api.async_register_command(hass, ws_reset)
    websocket_api.async_register_command(hass, ws_sessions)
    websocket_api.async_register_command(hass, ws_info)
    websocket_api.async_register_command(hass, ws_sql)
    websocket_api.async_register_command(hass, ws_sql_tables)


def _manager(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> SessionManager | None:
    if (manager := hass.data.get(DOMAIN)) is None:
        connection.send_error(
            msg["id"], "not_loaded", "Home Assistant REPL integration is not loaded"
        )
    return manager


@websocket_api.require_admin
# Home Assistant installs probatio as the `voluptuous` module itself
# (probatio.compat.install_as_voluptuous(), called from homeassistant's own
# __init__.py) - this schema and websocket_command()'s internal
# BASE_COMMAND_MESSAGE_SCHEMA.extend() both end up using the same probatio
# Marker classes at runtime, so this works. Static checkers don't know that:
# websocket_command()'s own signature is still annotated against real
# voluptuous's Marker/VolDictType, a type unrelated to probatio's from
# mypy/ty's point of view - hence the cast.
@websocket_api.websocket_command(
    cast(
        Any,
        {
            vol.Required("type"): "ha_repl_server/exec",
            vol.Required("code"): str,
            vol.Optional("session", default=DEFAULT_SESSION): str,
            vol.Optional("timeout"): vol.Coerce(float),
            vol.Optional("color", default=False): bool,
            vol.Optional("width", default=88): vol.Coerce(int),
            vol.Optional("auto_await", default=True): bool,
            vol.Optional("fetch", default=list): [str],
        },
    )
)
@websocket_api.async_response
async def ws_exec(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    # So an action in `obj` has its description, which is read where nothing
    # can be waited for. The code runs without it, just less well described.
    with contextlib.suppress(Exception):
        await async_load_action_descriptions(hass)
    result = await manager.get(msg["session"]).run(
        msg["code"],
        msg.get("timeout"),
        color=msg["color"],
        width=msg["width"],
        auto_await=msg["auto_await"],
        fetch=msg["fetch"],
    )
    connection.send_result(msg["id"], result.as_dict())


@websocket_api.require_admin
@websocket_api.websocket_command(
    cast(
        Any,
        {
            vol.Required("type"): "ha_repl_server/reset",
            vol.Optional("session", default=DEFAULT_SESSION): str,
        },
    )
)
@callback
def ws_reset(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], {"reset": manager.reset(msg["session"])})


@websocket_api.require_admin
@websocket_api.websocket_command(
    cast(Any, {vol.Required("type"): "ha_repl_server/sessions"})
)
@callback
def ws_sessions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], {"sessions": manager.describe()})


@websocket_api.require_admin
@websocket_api.websocket_command(
    cast(Any, {vol.Required("type"): "ha_repl_server/info"})
)
@websocket_api.async_response
async def ws_info(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The server-side component's own version - compared against the
    client's own package version in the live-mode shell's startup banner,
    since the two are versioned and deployed independently (PyPI package vs
    HACS component) and can drift out of sync."""
    integration = await async_get_integration(hass, DOMAIN)
    connection.send_result(msg["id"], {"version": integration.version})


def _sql_manager(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> SessionManager | None:
    """_manager(), plus the integration's own expose_sql switch."""
    if (manager := _manager(hass, connection, msg)) is None:
        return None
    if not manager.has_feature("sql"):
        connection.send_error(
            msg["id"],
            "sql_disabled",
            "sql is switched off in this integration's options",
        )
        return None
    return manager


@websocket_api.require_admin
@websocket_api.websocket_command(
    cast(
        Any,
        {
            vol.Required("type"): "ha_repl_server/sql",
            vol.Required("query"): str,
            vol.Optional("max_rows", default=DEFAULT_MAX_ROWS): vol.Any(
                None, vol.Coerce(int)
            ),
            vol.Optional("raw", default=False): bool,
        },
    )
)
@websocket_api.async_response
async def ws_sql(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Run one query and send the result back as Arrow IPC bytes (base64,
    since the websocket API is JSON) - the client's own `sql` is a local
    object calling this, not code exec'd here, so everything past the
    download (show(), dataframes, CSV) happens on the client's machine."""
    if _sql_manager(hass, connection, msg) is None:
        return
    try:
        result = await sql(hass, msg["query"], max_rows=msg["max_rows"], raw=msg["raw"])
    except SqlError as err:
        connection.send_error(msg["id"], "sql_error", str(err))
        return
    except Exception as err:  # noqa: BLE001 - a bad query is the caller's to see, whatever the driver raised
        connection.send_error(msg["id"], "sql_error", f"{type(err).__name__}: {err}")
        return
    connection.send_result(
        msg["id"],
        {
            "arrow": base64.b64encode(result.data).decode("ascii"),
            "truncated": result.truncated,
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    cast(Any, {vol.Required("type"): "ha_repl_server/sql_tables"})
)
@callback
def ws_sql_tables(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The recorder's tables and their columns, as plain data for the
    client's own `sql.tables`."""
    if _sql_manager(hass, connection, msg) is None:
        return
    connection.send_result(msg["id"], {"tables": table_schemas()})
