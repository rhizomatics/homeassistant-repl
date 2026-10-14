"""Tests for the server side of sql - one query in, Arrow IPC bytes out -
against a real (in-memory) recorder via pytest-homeassistant-custom-
component's `recorder_mock` fixture, since this needs Home Assistant's own
SQLAlchemy engine/schema, not a fake. What a client does with those bytes
is tests/test_sql_client.py's business."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

import nanoarrow as na
import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.ulid import ulid_to_bytes
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_repl_server.sql import (
    ArrowResult,
    SqlError,
    _add_virtual_tables,
    _recorder_column_kinds,
    sql,
    table_schemas,
)


async def _settle(hass: HomeAssistant) -> None:
    # The harness's own "everything is written" helper: it forces a commit
    # between waits, which a bare block_till_done on each side doesn't - a
    # state set just before could otherwise still be uncommitted when the
    # query runs.
    await async_wait_recording_done(hass)


def _batch(result: ArrowResult) -> Any:
    return na.ArrayStream.from_readable(result.data).read_all()


async def test_sql_select_returns_rows_as_arrow_ipc(recorder_mock, hass: HomeAssistant):
    hass.states.async_set("sensor.test", "42")
    await _settle(hass)

    # states.entity_id itself is a deprecated/unused column in the current
    # schema (the real value lives in states_meta, joined by metadata_id) -
    # a join like this, not a bare `select entity_id from states`, is what a
    # real query against this schema actually looks like.
    result = await sql(
        hass,
        "select states_meta.entity_id, states.state from states "
        "join states_meta on states.metadata_id = states_meta.metadata_id",
    )

    assert isinstance(result, ArrowResult)
    assert result.rowcount == 1
    assert not result.truncated
    assert _batch(result).to_pylist() == [{"entity_id": "sensor.test", "state": "42"}]


async def test_sql_column_types_from_recorder_schema(
    recorder_mock, hass: HomeAssistant
):
    hass.states.async_set("sensor.test", "42")
    await _settle(hass)

    result = await sql(hass, "select metadata_id from states_meta")
    # metadata_id is BigInteger in the current schema - should be a real
    # int64 column, not a generic/string fallback.
    (field,) = _batch(result).schema.fields
    assert (field.name, str(field.type)) == ("metadata_id", "Type.INT64")


async def test_sql_unknown_column_falls_back_to_value_inference(
    recorder_mock, hass: HomeAssistant
):
    result = await sql(hass, "select count(*) as n from states_meta")

    batch = _batch(result)
    assert batch.to_pylist() == [{"n": 0}]
    assert str(batch.schema.fields[0].type) == "Type.INT64"


async def test_sql_with_no_rows_still_carries_the_columns(
    recorder_mock, hass: HomeAssistant
):
    result = await sql(hass, "select entity_id from states_meta where 1 = 0")

    assert result.rowcount == 0
    assert [f.name for f in _batch(result).schema.fields] == ["entity_id"]


async def test_sql_max_rows_truncates_and_flags_it(recorder_mock, hass: HomeAssistant):
    for i in range(5):
        hass.states.async_set(f"sensor.test_{i}", "on")
    await _settle(hass)

    result = await sql(hass, "select entity_id from states_meta", max_rows=2)
    assert result.rowcount == 2
    assert result.truncated is True
    assert len(_batch(result)) == 2


async def test_sql_max_rows_none_removes_the_cap(recorder_mock, hass: HomeAssistant):
    result = await sql(
        hass, "select 1 as a union all select 2 union all select 3", max_rows=None
    )
    assert result.rowcount == 3
    assert result.truncated is False


async def test_sql_rejects_non_select(recorder_mock, hass: HomeAssistant):
    with pytest.raises(SqlError, match="only SELECT"):
        await sql(hass, "delete from states")


async def test_sql_rejects_multiple_statements(recorder_mock, hass: HomeAssistant):
    with pytest.raises(SqlError, match="single statement"):
        await sql(hass, "select 1; select 2")


async def test_sql_rejects_empty_query(recorder_mock, hass: HomeAssistant):
    with pytest.raises(SqlError, match="empty"):
        await sql(hass, "   ")


async def test_sql_rejects_bad_max_rows(recorder_mock, hass: HomeAssistant):
    with pytest.raises(SqlError, match="max_rows"):
        await sql(hass, "select 1", max_rows=0)


async def test_sql_state_history_has_entity_id_without_a_join(
    recorder_mock, hass: HomeAssistant
):
    hass.states.async_set("sensor.test", "42")
    hass.states.async_set("sensor.other", "7")
    await _settle(hass)

    result = await sql(
        hass, "select * from state_history where entity_id = 'sensor.test'"
    )

    rows = _batch(result).to_pylist()
    assert [(r["entity_id"], r["state"]) for r in rows] == [("sensor.test", "42")]
    names = list(rows[0])
    assert names[0] == "entity_id"
    assert "last_updated_ts" in names
    # neither the join key nor the legacy columns
    assert not {"metadata_id", "last_updated", "attributes", "event_id"} & set(names)


async def test_sql_state_history_joins_a_with_clause_and_itself(
    recorder_mock, hass: HomeAssistant
):
    hass.states.async_set("sensor.test", "1")
    hass.states.async_set("sensor.test", "2")
    await _settle(hass)

    result = await sql(
        hass,
        "with latest as (select max(last_updated_ts) as ts from state_history) "
        "select h.state, p.state as previous from state_history h "
        "join latest on latest.ts = h.last_updated_ts "
        "join state_history p on p.state_id = h.old_state_id",
    )

    assert _batch(result).to_pylist() == [{"state": "2", "previous": "1"}]


async def test_sql_event_history_has_event_type_without_a_join(
    recorder_mock, hass: HomeAssistant
):
    hass.bus.async_fire("test_happened", {"n": 1})
    await _settle(hass)

    result = await sql(
        hass, "select * from event_history where event_type = 'test_happened'"
    )

    rows = _batch(result).to_pylist()
    assert len(rows) == 1
    names = list(rows[0])
    assert names[0] == "event_type"
    assert {"time_fired_ts", "data_id"} <= set(names)
    assert not {"event_type_id", "time_fired", "event_data"} & set(names)


@pytest.mark.parametrize(
    "table", ["statistics_history", "statistics_short_term_history"]
)
async def test_sql_statistics_tables_have_statistic_id_without_a_join(
    recorder_mock, hass: HomeAssistant, table: str
):
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    async_add_external_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "Energy",
            "source": "test",
            "statistic_id": "test:energy",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [
            {"start": start - timedelta(hours=1), "state": 1.0, "sum": 1.0},
            {"start": start, "state": 3.0, "sum": 4.0},
        ],
    )
    await _settle(hass)

    result = await sql(
        hass, f"select * from {table} where statistic_id = 'test:energy'"
    )

    rows = _batch(result).to_pylist()
    names = [f.name for f in _batch(result).schema.fields]
    assert names[0] == "statistic_id"
    assert {"start_ts", "mean", "state", "sum"} <= set(names)
    assert not {"metadata_id", "start", "created"} & set(names)
    # external statistics are long term only
    expected = [4.0, 1.0] if table == "statistics_history" else []
    assert sorted((r["sum"] for r in rows), reverse=True) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("select 1 from states", "select 1 from states"),
        (
            "select 1 from state_history",
            "with state_history as (...) select 1 from state_history",
        ),
        (
            "select 1 from event_history e join state_history s",
            (
                "with state_history as (...), event_history as (...) "
                "select 1 from event_history e join state_history s"
            ),
        ),
        # a virtual table's name inside another's is not a mention of it
        (
            "select 1 from statistics_short_term_history",
            (
                "with statistics_short_term_history as (...) "
                "select 1 from statistics_short_term_history"
            ),
        ),
        (
            "-- recent\n  SELECT 1 FROM State_History",
            "-- recent\n  with state_history as (...) SELECT 1 FROM State_History",
        ),
        (
            "with a as (select 1 from state_history) select * from a",
            "with state_history as (...), a as (select 1 from state_history) select * from a",
        ),
        (
            "WITH RECURSIVE a as (select 1 from state_history) select * from a",
            (
                "WITH RECURSIVE state_history as (...), "
                "a as (select 1 from state_history) select * from a"
            ),
        ),
    ],
)
def test_add_virtual_tables_defines_those_mentioned_in_one_with_clause(query, expected):
    added = _add_virtual_tables(query, "mysql")
    assert re.sub(r"as \(select m\..*? = t\.\w+\)", "as (...)", added) == expected


def test_add_virtual_tables_stops_one_being_built_whole_where_that_can_be_said():
    assert " as not materialized (" in _add_virtual_tables(
        "select 1 from state_history", "sqlite"
    )
    assert " as not materialized (" in _add_virtual_tables(
        "select 1 from state_history", "postgresql"
    )
    assert "materialized" not in _add_virtual_tables(
        "select 1 from state_history", "mysql"
    )


def test_table_schemas_lists_virtual_tables_among_the_tables():
    tables = {t["name"]: t for t in table_schemas()}

    assert [n for n, t in tables.items() if t["virtual"]] == [
        "event_history",
        "state_history",
        "statistics_history",
        "statistics_short_term_history",
    ]
    assert tables["event_history"]["columns"][0]["name"] == "event_type"
    assert tables["statistics_history"]["columns"][0]["name"] == "statistic_id"
    virtual = tables["state_history"]
    assert virtual["virtual"]
    assert not tables["states"]["virtual"]
    names = [c["name"] for c in virtual["columns"]]
    assert names[0] == "entity_id"
    assert virtual["columns"][0]["type"] == "VARCHAR(255)"
    in_use = [c["name"] for c in tables["states"]["columns"] if not c["legacy"]]
    assert names[1:] == [n for n in in_use if n != "metadata_id"]
    assert not any(c["legacy"] for c in virtual["columns"])
    assert virtual["definition"].startswith("select m.entity_id, t.state_id, ")
    assert "definition" not in tables["states"]


def test_table_schemas_lists_current_recorder_tables_with_columns():
    tables = {t["name"]: t["columns"] for t in table_schemas()}

    assert {"states", "states_meta", "events", "statistics"} <= set(tables)
    assert {
        "name": "entity_id",
        "type": "VARCHAR(255)",
        "legacy": False,
    } in tables["states_meta"]
    assert {"name": "entity_id", "type": "CHAR", "legacy": True} in tables["states"]
    assert list(tables) == sorted(tables)
    described = {t["name"]: (t["class"], t["doc"]) for t in table_schemas()}
    assert described["states"] == ("States", "State change history.")


async def test_sql_gives_recorder_columns_what_db_schema_says_they_hold(
    recorder_mock, hass: HomeAssistant
):
    before = dt_util.utcnow() - timedelta(seconds=5)
    hass.states.async_set(
        "sensor.test", "42", context=Context(user_id=uuid.uuid4().hex)
    )
    await _settle(hass)
    state = hass.states.get("sensor.test")
    assert state is not None

    result = await sql(
        hass,
        "select last_updated_ts, context_id_bin, context_user_id_bin, "
        "context_parent_id_bin from state_history",
    )

    batch = _batch(result)
    assert [str(f.type) for f in batch.schema.fields] == [
        "Type.TIMESTAMP",
        "Type.STRING",
        "Type.STRING",
        "Type.STRING",
    ]
    (row,) = batch.to_pylist()
    # TIMESTAMP_TYPE: seconds since the epoch, sent as a time in UTC. The
    # recorder stores it as a float, so it can come back 1µs adrift
    assert abs(row["last_updated_ts"] - state.last_updated) <= timedelta(microseconds=1)
    assert row["last_updated_ts"].tzinfo is not None
    assert row["last_updated_ts"] > before
    # a context id is a ULID - all but the user's, which is a UUID
    assert row["context_id_bin"] == state.context.id
    assert row["context_user_id_bin"] == state.context.user_id
    assert row["context_parent_id_bin"] is None


async def test_sql_datetime_and_boolean_columns(recorder_mock, hass: HomeAssistant):
    await _settle(hass)

    result = await sql(hass, "select start, closed_incorrect from recorder_runs")

    batch = _batch(result)
    assert [str(f.type) for f in batch.schema.fields] == [
        "Type.TIMESTAMP",
        "Type.BOOL",
    ]
    row = batch.to_pylist()[0]
    # DATETIME_TYPE: text in SQLite, with no timezone, which is UTC
    assert abs(row["start"] - dt_util.utcnow()) < timedelta(hours=1)
    assert row["closed_incorrect"] is False


async def test_sql_raw_leaves_values_as_the_database_has_them(
    recorder_mock, hass: HomeAssistant
):
    hass.states.async_set("sensor.test", "42")
    await _settle(hass)
    state = hass.states.get("sensor.test")
    assert state is not None

    result = await sql(
        hass, "select last_updated_ts, context_id_bin from state_history", raw=True
    )
    runs = await sql(
        hass, "select start, closed_incorrect from recorder_runs", raw=True
    )

    (row,) = _batch(result).to_pylist()
    assert row["last_updated_ts"] == pytest.approx(state.last_updated.timestamp())
    assert row["context_id_bin"] == ulid_to_bytes(state.context.id)
    run = _batch(runs).to_pylist()[0]
    assert isinstance(run["start"], str)
    assert run["closed_incorrect"] == 0
    assert type(run["closed_incorrect"]) is int


async def test_sql_a_column_only_named_like_a_recorder_one_goes_by_its_values(
    recorder_mock, hass: HomeAssistant
):
    result = await sql(hass, "select 'soon' as start_ts, 'x' as context_id_bin")

    assert _batch(result).to_pylist() == [{"start_ts": "soon", "context_id_bin": "x"}]


def test_a_plain_double_is_not_taken_for_a_timestamp():
    # TIMESTAMP_TYPE and DOUBLE_TYPE are one object in db_schema
    kinds = _recorder_column_kinds()
    assert str(kinds["mean"].arrow.type) == "Type.DOUBLE"
    assert str(kinds["start_ts"].arrow.type) == "Type.TIMESTAMP"
