"""Tests for the client-side SqlResult (src/homeassistant_repl/sql.py) -
deliberately independent of custom_components/ha_repl_server/sql.py's own
tests (test_sql.py), since the two classes share nothing but the Arrow IPC
bytes one produces and the other reads - see sql.py's own module docstring.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import nanoarrow as na
import pytest
from rich.table import Table

from homeassistant_repl.client import HaReplError
from homeassistant_repl.sql import (
    SqlColumn,
    SqlError,
    SqlResult,
    SqlRow,
    SqlTable,
    SqlTool,
    SqlVirtualTable,
)


def _server_arrow_bytes(**columns: list) -> bytes:
    """Build the exact bytes custom_components/ha_repl_server/sql.py's own
    SqlResult.arrow() would produce for these columns, without importing
    that module (it needs a real `homeassistant` install) - the struct/IPC
    construction it uses internally, inlined here as the fixture.
    """
    import io

    from nanoarrow.ipc import StreamWriter

    arrays = {
        name: na.array(
            values, schema=na.string() if isinstance(values[0], str) else na.int64()
        )
        for name, values in columns.items()
    }
    schema = na.struct({name: arr.schema for name, arr in arrays.items()})
    batch = na.c_array_from_buffers(
        schema,
        length=len(next(iter(arrays.values()))),
        buffers=[],
        children=list(arrays.values()),
    )
    buf = io.BytesIO()
    with StreamWriter.from_writable(buf) as writer:
        writer.write_stream(batch)
    return buf.getvalue()


def _result(**columns: list) -> SqlResult:
    return SqlResult.from_arrow(_server_arrow_bytes(**columns))


def test_from_arrow_round_trips_columns_and_rowcount():
    data = _server_arrow_bytes(a=[1, 2, 3], b=["x", "y", "z"])

    result = SqlResult.from_arrow(data)

    assert result.rowcount == 3
    assert result.to_dicts() == [
        {"a": 1, "b": "x"},
        {"a": 2, "b": "y"},
        {"a": 3, "b": "z"},
    ]


def test_from_arrow_carries_truncated_out_of_band():
    data = _server_arrow_bytes(a=[1])

    assert SqlResult.from_arrow(data).truncated is False
    assert SqlResult.from_arrow(data, truncated=True).truncated is True


def test_arrow_round_trips_back_through_from_arrow():
    result = _result(a=[1, 2, 3])

    data = result.arrow_ipc()

    assert SqlResult.from_arrow(data).to_dicts() == result.to_dicts()


def test_iterates_rows_as_lists():
    result = _result(a=[1, 2, 3], b=["x", "y", "z"])

    assert list(result) == [[1, "x"], [2, "y"], [3, "z"]]


def test_len_reports_row_count():
    result = _result(a=[1, 2, 3])
    assert len(result) == 3


def test_project_returns_just_those_columns():
    result = _result(a=[1, 2, 3], b=["x", "y", "z"], c=[7, 8, 9])

    projected = result.project(["c", "a"])

    assert projected.column_names == ["c", "a"]
    assert projected.to_dicts() == [
        {"c": 7, "a": 1},
        {"c": 8, "a": 2},
        {"c": 9, "a": 3},
    ]


def test_slicing_returns_a_new_result():
    result = _result(a=list(range(10)))

    assert result[:3].to_dicts() == [{"a": 0}, {"a": 1}, {"a": 2}]
    assert result[-1:].to_dicts() == [{"a": 9}]
    assert result[:3].rowcount == 3


def test_indexing_returns_a_row():
    result = _result(a=[1, 2, 3], b=["x", "y", "z"])

    row = result[0]
    assert isinstance(row, SqlRow)
    assert row.values == [1, "x"]
    assert row.column_names == ["a", "b"]
    assert row.table is result.table
    assert (row[0], row["b"], len(row), list(row)) == (1, "x", 2, [1, "x"])
    assert row.to_dict() == {"a": 1, "b": "x"}
    assert repr(row) == "Row({'a': 1, 'b': 'x'})"
    assert result[-1].values == [3, "z"]
    assert result[1:][0].values == [2, "y"]
    with pytest.raises(KeyError, match="no column 'c' - columns are: a, b"):
        row["c"]


def test_indexing_rejects_what_is_not_a_row_number():
    result = _result(a=[1, 2, 3])
    with pytest.raises(IndexError, match="row 3 out of range - result has 3 rows"):
        result[3]
    with pytest.raises(TypeError, match="row number"):
        result["a"]  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]


def test_sample_caps_at_available_rows_and_keeps_order():
    result = _result(a=list(range(100)))

    sampled = result.sample(10)
    assert len(sampled) == 10
    values = [row["a"] for row in sampled.to_dicts()]
    assert len(set(values)) == 10
    assert values == sorted(values)

    assert len(result.sample(1000)) == 100


def test_show_returns_a_rich_table_with_defaults():
    result = _result(**{f"c{i}": [1] for i in range(8)})

    table = result.show()

    # every column a row has, so what's shown is what result[0] holds
    assert isinstance(table, Table)
    assert [str(c.header) for c in table.columns] == result[0].column_names
    assert "cols" not in str(table.caption)

    table = result.show(max_cols=6)
    assert [str(c.header) for c in table.columns] == [f"c{i}" for i in range(6)]
    assert "6/8 cols" in str(table.caption)


def test_show_max_rows_and_max_cols_override_defaults():
    result = _result(a=list(range(40)))

    table = result.show(max_rows=5)
    assert len(list(table.columns[0].cells)) == 5
    assert "showing first 5 rows" in str(table.caption)

    table = result.show(max_rows=None)
    assert len(list(table.columns[0].cells)) == 40


def test_export_csv_writes_header_and_rows(tmp_path):
    result = _result(a=[1, 2], b=["x", "y"])
    path = tmp_path / "out.csv"

    written = result.export_csv(path)

    assert written == path
    assert path.read_bytes() == b"a,b\r\n1,x\r\n2,y\r\n"


def test_export_csv_defaults_to_result_csv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _result(a=[1])

    path = result.export_csv()

    assert path == Path("result.csv")
    assert path.is_file()


def test_to_polars_builds_a_dataframe():
    pl = pytest.importorskip("polars")
    result = _result(a=[1, 2, 3])

    df = result.to_polars()

    assert isinstance(df, pl.DataFrame)
    assert df["a"].to_list() == [1, 2, 3]


def test_to_pandas_builds_a_dataframe():
    pd = pytest.importorskip("pandas")
    result = _result(a=[1, 2, 3])

    df = result.to_pandas()

    assert isinstance(df, pd.DataFrame)
    assert list(df["a"]) == [1, 2, 3]


class _FakeClient:
    """Stands in for the websocket Client: canned replies per command."""

    def __init__(self, arrow: bytes = b"", error: Exception | None = None) -> None:
        self.arrow = arrow
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, type_: str, **payload):
        self.calls.append((type_, payload))
        if type_ == "ha_repl_server/sql_tables":
            return {
                "tables": [
                    {
                        "name": "t",
                        "class": "Things",
                        "doc": "Thing history.",
                        "columns": [
                            {"name": "id", "type": "INTEGER"},
                            {"name": "name", "type": "VARCHAR(255)"},
                        ],
                    },
                    {
                        "name": "other",
                        "columns": [
                            {"name": "zzz", "type": "TEXT"},
                            {"name": "old", "type": "CHAR", "legacy": True},
                            {"name": "name", "type": "CHAR", "legacy": True},
                        ],
                    },
                ]
            }
        if self.error is not None:
            raise self.error
        return {
            "arrow": base64.b64encode(self.arrow).decode("ascii"),
            "truncated": True,
        }


async def test_sqltool_connect_downloads_tables():
    tool = await SqlTool.connect(_FakeClient())

    assert [t.name for t in tool.tables] == ["t", "other"]
    assert tool.tables[0].column_names == ["id", "name"]
    assert repr(tool.tables[0].columns[1]) == "Column('name', VARCHAR(255))"
    assert repr(tool.tables[0]) == "Table('t', columns=['id', 'name'])"
    assert (tool.tables[0].class_name, tool.tables[0].description) == (
        "Things",
        "Thing history.",
    )
    # a server that doesn't send them
    assert (tool.tables[1].class_name, tool.tables[1].description) == ("", "")


async def test_sqltool_connect_makes_virtual_tables_of_those_marked_so():
    class Client(_FakeClient):
        async def call(self, type_: str, **payload):
            reply = await super().call(type_, **payload)
            reply["tables"].append({
                "name": "t_history",
                "class": "",
                "doc": "Things, named.",
                "virtual": True,
                "definition": "select 1",
                "columns": [{"name": "id", "type": "INTEGER"}],
            })
            return reply

    tool = await SqlTool.connect(Client())

    assert [type(t) for t in tool.tables] == [SqlTable, SqlTable, SqlVirtualTable]
    assert [t.virtual for t in tool.tables] == [False, False, True]
    virtual = tool.table("t_history")
    assert isinstance(virtual, SqlVirtualTable)
    assert repr(virtual) == "VirtualTable('t_history', columns=['id'])"
    assert (virtual.description, virtual.definition) == ("Things, named.", "select 1")
    # asked for with legacy columns, it is still a virtual table
    assert isinstance(tool.table("t_history", legacy=True), SqlVirtualTable)


async def test_result_of_a_virtual_table_is_described_by_it_not_its_tables():
    states = SqlTable(
        "states",
        (
            SqlColumn("entity_id", "CHAR", legacy=True),
            SqlColumn("state", "VARCHAR(255)"),
            SqlColumn("metadata_id", "BIGINT"),
        ),
    )
    virtual = SqlVirtualTable(
        "state_history",
        (SqlColumn("entity_id", "VARCHAR(255)"), SqlColumn("state", "VARCHAR(255)")),
    )
    client = _FakeClient(_server_arrow_bytes(entity_id=["sensor.a"], state=["1"]))
    tool = SqlTool(client, tables=[virtual, states])

    result = await tool("select * from State_History")
    assert result.table is virtual
    assert result.column_names == ["entity_id", "state"]
    # and a table's result is still the table's, though the virtual one has
    # its columns
    assert (await tool("select state from states")).table is states


async def test_table_has_legacy_columns_only_when_asked():
    tool = await SqlTool.connect(_FakeClient())
    other = tool.table("other")

    assert other is tool.tables[1]
    assert other.column_names == ["zzz"]
    assert [(c.name, c.legacy) for c in other.columns] == [("zzz", False)]
    assert repr(other) == "Table('other', columns=['zzz'])"

    full = tool.table("other", legacy=True)
    assert full.column_names == ["zzz", "old", "name"]
    assert [(c.name, c.legacy) for c in full.columns] == [
        ("zzz", False),
        ("old", True),
        ("name", True),
    ]
    assert repr(full.columns[1]) == "Column('old', CHAR, legacy)"
    # asking for them doesn't change the table everyone else gets
    assert tool.table("other").column_names == ["zzz"]


@pytest.mark.parametrize(
    ("query", "kwargs", "expected"),
    [
        ("select * from other", {}, ["zzz"]),
        ("select * from other", {"legacy": True}, ["zzz", "old", "name"]),
        ("select zzz, old from other", {}, ["zzz", "old"]),
        ("select * from other where OLD is null", {}, ["zzz", "old"]),
        # `name` is in use in t, so isn't hidden once t is in the query
        ("select * from other join t", {}, ["zzz", "name"]),
        # no table of ours named: nothing is known to be legacy
        ("select * from elsewhere", {}, ["zzz", "old", "name"]),
    ],
)
async def test_sqltool_leaves_out_unnamed_legacy_columns(query, kwargs, expected):
    client = _FakeClient(_server_arrow_bytes(zzz=[1], old=[None], name=[None]))
    tool = await SqlTool.connect(client)

    result = await tool(query, **kwargs)

    assert result.column_names == expected
    assert result.table is tool.table("other")
    # nothing was dropped, only kept out of virtual
    result.legacy = True
    assert result.column_names == ["zzz", "old", "name"]


async def test_result_legacy_flag_applies_to_every_operation(tmp_path):
    client = _FakeClient(
        _server_arrow_bytes(zzz=[1, 2, 3], old=["a", "b", "c"], name=[None] * 3)
    )
    result = await (await SqlTool.connect(client))("select * from other")

    def views(r: SqlResult) -> list:
        shown = r.show()
        return [
            r.column_names,
            r[0].column_names,
            [column.header for column in shown.columns],
            list(r.to_dicts()[0]),
            r.json_data()["columns"],
            [f.name for f in r.arrow().schema.fields],
            SqlResult.from_arrow(r.arrow_ipc()).column_names,
            r[1:].column_names,
            r.sample(2).column_names,
            r.export_csv(tmp_path / "r.csv").read_text().splitlines()[0].split(","),
        ]

    assert views(result) == [["zzz"]] * 10
    assert (list(result), result[1].values, result[1:][0].values) == (
        [[1], [2], [3]],
        [2],
        [2],
    )
    assert repr(result) == "<SqlResult 3 rows x 1 cols [zzz] (truncated)>"

    # a legacy column asked for by name is in view from then on
    assert result.project(["zzz", "old"]).column_names == ["zzz", "old"]
    assert [c.header for c in result.show(columns=["old"]).columns] == ["old"]

    part = result[1:]
    result.legacy = True
    assert views(result) == [["zzz", "old", "name"]] * 10
    assert result[1].values == [2, "b", None]
    # a result made from it earlier has its own flag
    assert part.column_names == ["zzz"]
    part.legacy = True
    assert part[0].values == [2, "b", None]


async def test_sqltool_table_finds_a_table_by_name():
    tool = await SqlTool.connect(_FakeClient())

    assert tool.table("other") is tool.tables[1]
    with pytest.raises(KeyError, match="no table 'nope' - tables are: t, other"):
        tool.table("nope")


async def test_sqltool_call_returns_a_local_result_with_its_table():
    client = _FakeClient(_server_arrow_bytes(id=[1, 2], name=["a", "b"]))
    tool = await SqlTool.connect(client)

    result = await tool("select * from t")

    assert isinstance(result, SqlResult)
    assert list(result) == [[1, "a"], [2, "b"]]
    assert result.truncated is True
    assert result.table is tool.tables[0]
    assert result[:1].table is tool.tables[0]
    assert client.calls[-1] == (
        "ha_repl_server/sql",
        {"query": "select * from t", "max_rows": 1000},
    )


async def test_sqltool_max_rows_default_and_per_call_override():
    client = _FakeClient(_server_arrow_bytes(id=[1]))
    tool = await SqlTool.connect(client)

    tool.max_rows = 5
    await tool("select 1")
    await tool("select 1", max_rows=None)

    assert [c[1]["max_rows"] for c in client.calls[1:]] == [5, None]


async def test_sqltool_turns_a_server_sql_error_into_sqlerror():
    error = HaReplError(
        "ha_repl_server/sql failed: only SELECT queries are allowed",
        code="sql_error",
        detail="only SELECT queries are allowed",
    )
    tool = await SqlTool.connect(_FakeClient(error=error))

    with pytest.raises(SqlError, match=r"^only SELECT queries are allowed$"):
        await tool("delete from t")


async def test_sqltool_leaves_other_failures_alone():
    tool = await SqlTool.connect(_FakeClient(error=HaReplError("Connection closed")))

    with pytest.raises(HaReplError, match="Connection closed"):
        await tool("select 1")


def test_export_csv_default_path_is_named_after_the_table(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    table = SqlTable("states", (SqlColumn("id", "INTEGER"),))
    result = SqlResult.from_arrow(_server_arrow_bytes(id=[1]), tables=[table])

    assert result.export_csv().name == "states.csv"


def test_arrow_is_a_struct_array_other_libraries_can_import():
    result = _result(a=[1, 2], b=["x", "y"])

    array = result.arrow()

    assert hasattr(array, "__arrow_c_array__")
    assert hasattr(array, "__arrow_c_stream__")
    assert [f.name for f in array.schema.fields] == ["a", "b"]
    assert array.to_pylist() == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def test_arrow_feeds_polars_from_arrow():
    pl = pytest.importorskip("polars")
    result = _result(a=[1, 2], b=["x", "y"])

    df = pl.from_arrow(result.arrow())

    assert df.to_dicts() == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def test_result_has_no_rich_rendering_of_its_own():
    # Only show() draws a table; a bare result at the prompt is its repr.
    result = _result(id=[1])

    assert not hasattr(result, "__rich__")
    assert repr(result) == "<SqlResult 1 rows x 1 cols [id]>"


def test_to_json_is_columns_and_rows():
    result = _result(a=[1, 2], b=["x", "y"])

    assert json.loads(result.to_json()) == {
        "columns": ["a", "b"],
        "rows": [[1, "x"], [2, "y"]],
        "rowcount": 2,
        "truncated": False,
    }


def test_json_data_makes_binary_values_hex():
    column = na.array([b"\x01\xff", None], na.binary())
    batch = na.c_array_from_buffers(
        na.struct({"b": column.schema}), length=2, buffers=[], children=[column]
    )
    result = SqlResult(na.Array(batch))

    assert result.json_data()["rows"] == [["01ff"], [None]]


def test_arrow_is_the_downloaded_array_itself_not_a_rebuild():
    result = _result(a=[1, 2])

    assert result.arrow() is result.arrow()


def test_contiguous_slice_keeps_working_as_a_full_result():
    result = _result(a=[1, 2, 3, 4], b=["w", "x", "y", "z"])[1:3]

    assert result.rowcount == 2
    assert result.to_dicts() == [{"a": 2, "b": "x"}, {"a": 3, "b": "y"}]
    assert list(result.project(["b"])) == [["x"], ["y"]]
    assert list(result[-1:]) == [[3, "y"]]
    assert list(SqlResult.from_arrow(result.arrow_ipc())) == [[2, "x"], [3, "y"]]


def test_empty_and_out_of_range_slices():
    result = _result(a=[1, 2, 3, 4])

    assert len(result[3:1]) == 0
    assert result[10:].column_names == ["a"]
    assert len(result[10:][:5]) == 0


def test_stepped_slice_points_at_polars():
    with pytest.raises(ValueError, match="to_polars"):
        _result(a=[1, 2, 3, 4])[::2]


async def test_result_carries_the_query_it_came_from():
    tool = await SqlTool.connect(_FakeClient(_server_arrow_bytes(id=[1, 2])))

    result = await tool("select id from t")

    assert result.query == "select id from t"
    assert result[:1].query == result.project(["id"]).query == "select id from t"
    assert _result(id=[1]).query == ""


async def test_sqltool_asks_for_raw_only_when_told_to():
    client = _FakeClient(_server_arrow_bytes(id=[1]))
    tool = await SqlTool.connect(client)

    await tool("select 1")
    await tool("select 1", raw=True)

    assert [c[1].get("raw") for c in client.calls[1:]] == [None, True]


def test_a_time_column_reads_as_datetimes_and_survives_a_copy():
    from datetime import UTC, datetime

    when = datetime(2026, 10, 11, 12, 30, 0, 123456, tzinfo=UTC)
    micros = 1_791_721_800_123_456
    times = na.array([micros, None, micros + 1], na.timestamp("us", timezone="UTC"))
    schema = na.struct({"start_ts": times.schema})
    batch = na.c_array_from_buffers(schema, length=3, buffers=[], children=[times])
    result = SqlResult(na.Array(batch))

    assert result[0]["start_ts"] == when
    assert result.json_data()["rows"][0] == ["2026-10-11T12:30:00.123456+00:00"]
    # sample() and a sliced arrow_ipc() rebuild the column from its values
    assert list(result.sample(3)) == list(result)
    assert list(SqlResult.from_arrow(result[1:].arrow_ipc())) == list(result)[1:]
