"""Websocket commands: ha_repl_server/exec, ha_repl_server/cancel,
ha_repl_server/complete, ha_repl_server/reset, ha_repl_server/sessions, ha_repl_server/info,
ha_repl_server/sql and ha_repl_server/sql_tables (all admin only).

Every command that runs or stops something - exec, cancel, reset and sql - is
logged at WARNING, so it shows in Home Assistant's log by default, with the
user who sent it, and how it went with its timing - on its own logger,
COMMAND_LOG, to be quietened without hiding the integration's other warnings.
complete runs nothing, so is logged at DEBUG only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from typing import Any, cast

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .complete import complete, describe, signature, source
from .const import DEFAULT_SESSION, DOMAIN
from .objtree import async_load_action_descriptions
from .session import SessionManager
from .sql import DEFAULT_MAX_ROWS, SqlError, sql, table_schemas

_LOGGER = logging.getLogger(__package__)
COMMAND_LOG = logging.getLogger(f"{__package__}.commands")


# What a client can ask this server for, given in ha_repl_server/info so a
# newer client can tell what an older server lacks.
COMMANDS = frozenset({
    "exec",
    "cancel",
    "complete",
    "reset",
    "sessions",
    "info",
    "sql",
    "sql_tables",
})


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_exec)
    websocket_api.async_register_command(hass, ws_cancel)
    websocket_api.async_register_command(hass, ws_complete)
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


def _who(connection: websocket_api.ActiveConnection) -> str:
    user = connection.user
    return user.name or user.id if user else "unknown user"


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
    who, session = _who(connection), msg["session"]
    COMMAND_LOG.warning("%s runs in session %r:\n%s", who, session, msg["code"])
    try:
        result = await manager.get(session).run(
            msg["code"],
            msg.get("timeout"),
            color=msg["color"],
            width=msg["width"],
            auto_await=msg["auto_await"],
            fetch=msg["fetch"],
        )
    except asyncio.CancelledError:
        COMMAND_LOG.warning(
            "%s's run in session %r stopped: connection closed", who, session
        )
        raise
    COMMAND_LOG.warning("%s's run in session %r %s", who, session, result.summary())
    connection.send_result(msg["id"], result.as_dict())


@websocket_api.require_admin
@websocket_api.websocket_command(
    cast(
        Any,
        {
            vol.Required("type"): "ha_repl_server/cancel",
            vol.Optional("session"): str,
        },
    )
)
@callback
def ws_cancel(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Stop the code running in a session, or in every session if none is
    given - what the shell sends on Ctrl-C. The stopped run still answers its
    own exec, with a CancelledError."""
    if (manager := _manager(hass, connection, msg)) is None:
        return
    who = _who(connection)
    stopped = manager.cancel(msg.get("session"), f"Stopped by {who}")
    COMMAND_LOG.warning(
        "%s stopped %s",
        who,
        ", ".join(f"session {name!r}" for name in stopped) or "nothing (none running)",
    )
    connection.send_result(msg["id"], {"stopped": stopped})


@websocket_api.require_admin
@websocket_api.websocket_command(
    cast(
        Any,
        {
            vol.Required("type"): "ha_repl_server/complete",
            vol.Required("source"): str,
            vol.Required("cursor"): vol.Coerce(int),
            vol.Optional("session", default=DEFAULT_SESSION): str,
            vol.Optional("kind", default="completions"): vol.In([
                "completions",
                "signature",
                "source",
                "doc",
            ]),
        },
    )
)
@websocket_api.async_response
async def ws_complete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """What can go at the cursor, from the session's own namespace - see
    complete.py. Runs nothing, so it takes no lock and answers while an
    exec is running, and on the event loop, where the registries the
    object tree reads can be read. `resolved` is False when the expression
    before the cursor couldn't be followed without running something.

    With `kind: "signature"`, the signature of the call the cursor is in
    instead, or None. With `kind: "source"`, where the source of what
    `source` names is, with a GitHub link at the version running, or None -
    for `thing??`. With `kind: "doc"`, what `source` names: its type,
    signature and docstring - for Ctrl-Space and `ha-repl inspect`."""
    if (manager := _manager(hass, connection, msg)) is None:
        return
    namespace = manager.namespace(msg["session"])
    if msg["kind"] == "source":
        integration = await async_get_integration(hass, DOMAIN)
        versions = {"custom_components.ha_repl_server": str(integration.version)}
        found_source = source(msg["source"], namespace, versions)
        connection.send_result(
            msg["id"], {"source": found_source.as_dict() if found_source else None}
        )
        return
    if msg["kind"] == "doc":
        found_doc = describe(msg["source"], namespace)
        connection.send_result(
            msg["id"], {"doc": found_doc.as_dict() if found_doc else None}
        )
        return
    if msg["kind"] == "signature":
        found = signature(msg["source"], msg["cursor"], namespace)
        connection.send_result(
            msg["id"], {"signature": found.as_dict() if found else None}
        )
        return
    result = complete(
        msg["source"],
        msg["cursor"],
        namespace,
        limit=manager.complete_limit,
        budget=manager.complete_budget,
    )
    _LOGGER.debug("%s completes %r", _who(connection), msg["source"][: msg["cursor"]])
    if result is None:
        connection.send_result(
            msg["id"], {"items": [], "replace": 0, "resolved": False}
        )
        return
    connection.send_result(msg["id"], {**result.as_dict(), "resolved": True})


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
    reset = manager.reset(msg["session"])
    COMMAND_LOG.warning("%s reset session %r", _who(connection), msg["session"])
    connection.send_result(msg["id"], {"reset": reset})


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
    connection.send_result(
        msg["id"], {"version": integration.version, "commands": sorted(COMMANDS)}
    )


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
    who = _who(connection)
    COMMAND_LOG.warning("%s queries:\n%s", who, msg["query"])
    start = time.perf_counter()
    try:
        result = await sql(hass, msg["query"], max_rows=msg["max_rows"], raw=msg["raw"])
    except Exception as err:  # noqa: BLE001 - a bad query is the caller's to see, whatever the driver raised
        message = (
            str(err) if isinstance(err, SqlError) else f"{type(err).__name__}: {err}"
        )
        COMMAND_LOG.warning(
            "%s's query failed in %.3fs: %s", who, time.perf_counter() - start, message
        )
        connection.send_error(msg["id"], "sql_error", message)
        return
    COMMAND_LOG.warning(
        "%s's query returned %d rows in %.3fs%s",
        who,
        result.rowcount,
        time.perf_counter() - start,
        ", truncated" if result.truncated else "",
    )
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
