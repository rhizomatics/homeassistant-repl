"""The client-side half of `sql` results: a separate, independent SqlResult
from custom_components/ha_repl_server/sql.py's, sharing nothing but the
Arrow IPC bytes `.arrow()` produces there and `SqlResult.from_arrow()`
reads here - see this project's docs/developer/design/design_principles.md
("maximum freedom for the developer on the client, maximum restrictions on
the server side"). The server's own job stops at running the query and
downloading a complete (`max_rows`-capped, not streamed - HA's own tables
are small enough that lazy/paged loading isn't worth the complexity yet)
in-memory Arrow dataset; everything past that - rendering (`.show()`),
dataframe conversion (`.to_pandas()`/`.to_polars()`), CSV export - runs
here, against that already-downloaded data, so installing pandas/polars
(or anything else) is a `pip install` in your own environment, never a
change to the running Home Assistant instance.

Once downloaded, a SqlResult behaves like a wrapped local database handle
in its own right (think `sqlite3`'s cursor, except the storage underneath
is an Arrow buffer, not a `.db` file) - every operation below (slicing,
`.project()`, `.sample()`, iteration, ...) works off that one in-memory
copy, no further round trip to Home Assistant.
"""

from __future__ import annotations

import base64
import csv
import dataclasses
import io
import json
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import nanoarrow as na
from nanoarrow.ipc import StreamWriter
from rich.table import Table

from .client import HaReplError

DEFAULT_MAX_ROWS = 1000
DEFAULT_SHOW_ROWS = 30


class SqlError(Exception):
    """A rejected or failed query - not a bug in the shell itself."""


@dataclass(frozen=True)
class SqlColumn:
    """One column of a recorder table: its name and SQL type, as text."""

    name: str
    type: str
    legacy: bool = False
    """True for a column Home Assistant still has in the table but no
    longer writes to (declared UNUSED_LEGACY_* in the recorder's
    db_schema) - its values have moved elsewhere, e.g. `states.entity_id`
    to `states_meta`."""

    def __repr__(self) -> str:
        return f"Column({self.name!r}, {self.type}{', legacy' if self.legacy else ''})"


@dataclass(frozen=True)
class SqlTable:
    """A recorder table, as described by the server - plain data, not a
    live handle on anything."""

    name: str
    _columns: tuple[SqlColumn, ...]
    class_name: str = ""
    """The class Home Assistant's recorder maps to this table, e.g.
    `States` - found in homeassistant.components.recorder.db_schema."""
    description: str = ""
    """That class's docstring."""
    legacy: bool = False
    """Whether `columns` and `column_names` take in the legacy columns
    as well as those in use - as `sql.table(name, legacy=True)` gives."""

    @property
    def column_names(self) -> list[str]:
        """Just the names, in schema order - what a `select *` result
        shows, legacy columns among them only if `legacy`."""
        return [column.name for column in self.columns]

    @property
    def columns(self) -> list[SqlColumn]:
        """Name and type of each column, in schema order - legacy ones
        among them only if `legacy`."""
        return [c for c in self._columns if self.legacy or not c.legacy]

    @property
    def virtual(self) -> bool:
        """Whether this is a virtual table - a ready-made join to query
        like a table - rather than a real table in the database."""
        return False

    def __repr__(self) -> str:
        return f"Table({self.name!r}, columns={self.column_names!r})"


@dataclass(frozen=True, repr=False)
class SqlVirtualTable(SqlTable):
    """A virtual table: a ready-made join the server offers under this
    name, to query as if it were a table. It is not a table in the
    database - `state_history`, say, is `states` with each row's
    `entity_id` from `states_meta` - and has no class in db_schema."""

    definition: str = ""
    """The query this name stands for."""

    @property
    def virtual(self) -> bool:
        """Whether this is a virtual table - a ready-made join to query
        like a table - rather than a real table in the database."""
        return True

    def __repr__(self) -> str:
        return f"VirtualTable({self.name!r}, columns={self.column_names!r})"


@dataclass(frozen=True)
class SqlRow:
    """One row of a SqlResult, as `result[0]` gives it: its values in
    column order, plus the column names and table they came from. Reads
    like a list (`row[0]`, `len(row)`, a `for` loop) and by column name
    (`row["entity_id"]`).
    """

    values: list[Any]
    column_names: list[str]
    table: SqlTable | None = None
    """The recorder table of the result this row is from, if it had one."""

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            try:
                return self.values[self.column_names.index(key)]
            except ValueError:
                raise KeyError(
                    f"no column {key!r} - columns are: {', '.join(self.column_names)}"
                ) from None
        return self.values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def to_dict(self) -> dict[str, Any]:
        return dict(zip(self.column_names, self.values, strict=True))

    def json_data(self) -> list[Any]:
        """The values as JSON-ready data, as SqlResult.json_data() has them."""
        return [_json_value(v) for v in self.values]

    def __repr__(self) -> str:
        return f"Row({self.to_dict()!r})"


@dataclass
class SqlResult:
    """A query result downloaded from live mode's `sql`, held as the one
    Arrow struct array it arrived as (one field per column) - nothing is
    unpacked or copied until a method asks for Python values. Build one
    with `SqlResult.from_arrow()`.
    """

    _data: Any
    truncated: bool = False
    table: SqlTable | None = None
    """The recorder table these columns come from, when exactly one table
    has them all - None for a join, an aggregate, or a result that didn't
    come from `sql` in the first place."""
    legacy: bool = False
    """Whether legacy columns the query didn't name are part of this
    result. They are always downloaded, and left out of everything -
    `show()`, rows, `column_names`, dataframes, exports - until this is
    set to True, which can be done at any time."""
    _hidden: frozenset[str] = frozenset()
    """The columns `legacy` decides on."""
    query: str = ""
    """The query this is the result of, as it was given to `sql` - empty
    for a result that didn't come from `sql`."""

    @classmethod
    def from_arrow(
        cls,
        data: bytes,
        *,
        truncated: bool = False,
        tables: list[SqlTable] | None = None,
        legacy: bool = False,
        hidden: frozenset[str] = frozenset(),
        query: str = "",
    ) -> SqlResult:
        """Rebuild a SqlResult from Arrow IPC stream bytes (the one shared
        contract with the server that produced them) - a single
        struct-typed batch, one named field per column. `truncated` isn't
        itself encoded in the Arrow data (it's metadata about the query,
        not the rows), so it travels alongside the bytes rather than
        inside them. `hidden` names the columns left out unless `legacy`,
        and `query` is the query the bytes are the result of.
        """
        batch = na.ArrayStream.from_readable(data).read_all()
        names = {field.name for field in batch.schema.fields}
        matches = [t for t in tables or () if names <= {c.name for c in t._columns}]
        table = matches[0] if len(matches) == 1 else None
        return cls(batch, truncated, table, legacy, hidden, query)

    @classmethod
    def _from_columns(
        cls, columns: dict[str, Any], rowcount: int, like: SqlResult
    ) -> SqlResult:
        """A result over these per-column arrays, sharing their buffers."""
        schema = na.struct({name: arr.schema for name, arr in columns.items()})
        batch = na.c_array_from_buffers(
            schema, length=rowcount, buffers=[], children=list(columns.values())
        )
        return dataclasses.replace(like, _data=na.Array(batch))

    def _shown(self) -> SqlResult:
        """This result if all its columns are in view, else one over just
        those that are - which every reading operation works from."""
        names = self.column_names
        if len(names) == len(self._data.schema.fields):
            return self
        return self.project(names)

    def arrow(self) -> Any:
        """The whole result as one Arrow struct array (a `nanoarrow.Array`,
        one field per column), without copying. It implements the Arrow
        PyCapsule interface, so Arrow-aware libraries take it directly:
        `polars.DataFrame(r.arrow())`, `pyarrow.table(r.arrow())`,
        `pandas.DataFrame.from_arrow(r.arrow())`.
        """
        return self._shown()._data

    def arrow_ipc(self) -> bytes:
        """The result serialized as an Arrow IPC stream - the format
        `from_arrow()` reads, and what `polars.read_ipc_stream()` or
        `pyarrow.ipc.open_stream()` expect, e.g. to save to a file.
        """
        shown = self._shown()
        data = shown._data
        if data.n_chunks == 1 and data.offset:
            # The IPC writer can't encode a sliced (offset) array; write a
            # compact copy of just these rows instead.
            data = shown._take(range(self.rowcount))._data
        buf = io.BytesIO()
        with StreamWriter.from_writable(buf) as writer:
            writer.write_stream(data)
        return buf.getvalue()

    @property
    def rowcount(self) -> int:
        return len(self._data)

    @property
    def column_names(self) -> list[str]:
        return [
            field.name
            for field in self._data.schema.fields
            if self.legacy or field.name not in self._hidden
        ]

    def to_dicts(self) -> list[dict[str, Any]]:
        return self._shown()._data.to_pylist()

    def to_json(self, **kwargs: Any) -> str:
        """The whole result as one JSON document: `columns` (names),
        `rows` (a list per row, in column order), `rowcount` and
        `truncated`. Compact compared to to_dicts() since column names
        aren't repeated per row. `kwargs` go to json.dumps (indent, ...).
        """
        return json.dumps(self.json_data(), **kwargs)

    def json_data(self) -> dict[str, Any]:
        """What to_json() serializes, as plain Python data. Binary values
        become hex strings; anything else JSON has no type for, str()."""
        return {
            "columns": self.column_names,
            "rows": [[_json_value(v) for v in row] for row in self._rows()],
            "rowcount": self.rowcount,
            "truncated": self.truncated,
        }

    def to_polars(self) -> Any:
        import polars as pl  # type: ignore[import-not-found]  # ty: ignore[unresolved-import]

        return pl.DataFrame(self.arrow())

    def to_pandas(self) -> Any:
        import pandas as pd  # type: ignore[import-untyped]  # ty: ignore[unresolved-import]

        return pd.DataFrame({
            name: column.to_pylist() for name, column in self._column_arrays().items()
        })

    def __iter__(self) -> Iterator[list[Any]]:
        """Rows as plain lists, in column order - so a `for` loop or
        comprehension over a result just works."""
        for row in self._rows():
            yield list(row)

    def show(
        self,
        columns: list[str] | None = None,
        *,
        max_rows: int | None = DEFAULT_SHOW_ROWS,
        max_cols: int | None = None,
    ) -> Table:
        """A rich Table rendering of this result. Shows the same columns
        a row of it has (`result[0]`), unless `columns` names the ones to
        show or `max_cols` caps how many; at most `max_rows` rows.
        """
        all_names = self.column_names
        if columns is not None:
            names = columns
            cols_truncated = False
        elif max_cols is not None and len(all_names) > max_cols:
            names = all_names[:max_cols]
            cols_truncated = True
        else:
            names = all_names
            cols_truncated = False

        if max_rows is not None and self.rowcount > max_rows:
            row_cap = max_rows
            rows_truncated = True
        else:
            row_cap = self.rowcount
            rows_truncated = False

        caption = f"{self.rowcount} row{'' if self.rowcount == 1 else 's'}"
        if self.truncated:
            caption += " (truncated)"
        if cols_truncated:
            caption += f" ({len(names)}/{len(all_names)} cols)"
        if rows_truncated:
            caption += f" (showing first {row_cap} rows)"

        table = Table(*names, caption=caption)
        for i, row in enumerate(self._rows(names)):
            if i >= row_cap:
                break
            table.add_row(*("" if v is None else str(v) for v in row))
        return table

    def project(self, columns: list[str]) -> SqlResult:
        """A new SqlResult with just these columns (same rows, same
        underlying arrays - no data is copied). A legacy column named
        here is in view in the new result.
        """
        arrays = self._all_arrays()
        projected = self._from_columns(
            {name: arrays[name] for name in columns}, self.rowcount, self
        )
        projected._hidden = self._hidden - set(columns)
        return projected

    def __getitem__(self, key: int | slice) -> Any:
        """One row by position (`r[0]`, `r[-1]`) as a SqlRow, or a
        contiguous run of rows with standard Python slice notation
        (`r[:10]`, `r[-1:]`, `r[10:20]`) - a new SqlResult sharing this
        one's data, nothing copied. For anything more (a step, a filter,
        a sort) use a dataframe: `r.to_polars()`.
        """
        if isinstance(key, int) and not isinstance(key, bool):
            index = key + self.rowcount if key < 0 else key
            if not 0 <= index < self.rowcount:
                raise IndexError(
                    f"row {key} out of range - result has {self.rowcount} "
                    f"row{'' if self.rowcount == 1 else 's'}"
                )
            arrays = self._column_arrays()
            return SqlRow(
                [column[index].as_py() for column in arrays.values()],
                list(arrays),
                self.table,
            )
        if not isinstance(key, slice):
            raise TypeError(
                "SqlResult takes a row number (result[0]) or a slice "
                f"(result[:10]), not {key!r}"
            )
        start, stop, step = key.indices(self.rowcount)
        if step != 1:
            raise ValueError(
                "SqlResult slices can't have a step - use result.to_polars() "
                "for anything beyond a contiguous run of rows"
            )
        if self._data.n_chunks != 1:  # nothing to share: an empty result
            return self
        sliced = na.c_array(self._data)[start : max(start, stop)]
        return dataclasses.replace(self, _data=na.Array(sliced))

    def __len__(self) -> int:
        return self.rowcount

    def sample(self, count: int = 20) -> SqlResult:
        """A new SqlResult with `count` rows chosen at random, without
        replacement (capped at the rows actually here). Rows keep their
        original relative order, only which ones are picked is random.
        """
        return self._take(
            sorted(random.sample(range(self.rowcount), min(count, self.rowcount)))
        )

    def _take(self, indices: Any) -> SqlResult:
        """A new SqlResult holding copies of just these rows, in this order
        - only the rows asked for are read, not the whole column."""
        columns = {
            name: na.array([_stored(column[i].as_py()) for i in indices], column.schema)
            for name, column in self._all_arrays().items()
        }
        return self._from_columns(columns, len(indices), self)

    def export_csv(self, path: str | Path | None = None, **kwargs: Any) -> Path:
        """Write this result to a CSV file at `path` (your own machine,
        not Home Assistant's) - a header row, then every row with each
        cell through str(). Without a `path` it's named after `.table`
        ("states.csv"), or "result.csv" when there isn't one. `kwargs` go
        straight to csv.writer (dialect, delimiter, ...). The path actually
        written to is returned.
        """
        if path is None:
            path = f"{self.table.name if self.table else 'result'}.csv"
        path = Path(path)
        with path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp, **kwargs)
            writer.writerow(self.column_names)
            for row in self._rows():
                writer.writerow("" if v is None else str(v) for v in row)
        return path

    def _column_arrays(self) -> dict[str, Any]:
        """Each column in view as its own array - views into the struct,
        not copies."""
        arrays = self._all_arrays()
        return {name: arrays[name] for name in self.column_names}

    def _all_arrays(self) -> dict[str, Any]:
        """As _column_arrays(), whether or not the column is in view."""
        children = list(self._data.iter_children())
        if self._data.n_chunks == 1 and (
            self._data.offset or any(len(c) != self.rowcount for c in children)
        ):
            # A sliced struct keeps its offset/length on itself; its child
            # arrays still span the original rows, so narrow them to match.
            start, stop = self._data.offset, self._data.offset + self.rowcount
            children = [na.Array(na.c_array(c)[start:stop]) for c in children]
        names = [field.name for field in self._data.schema.fields]
        return dict(zip(names, children, strict=True))

    def _rows(self, names: list[str] | None = None) -> Iterator[tuple[Any, ...]]:
        if names is None:
            return self._shown()._data.iter_tuples()
        arrays = self._all_arrays()
        return zip(*(arrays[name].iter_py() for name in names), strict=True)

    def __repr__(self) -> str:
        suffix = " (truncated)" if self.truncated else ""
        names = self.column_names
        return (
            f"<SqlResult {self.rowcount} rows x {len(names)} cols "
            f"[{', '.join(names)}]{suffix}>"
        )


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _stored(value: Any) -> Any:
    """A value read out of an Arrow column, as building one takes it: a
    time as the microseconds since the epoch the server sends it as."""
    if isinstance(value, datetime):
        return (value - _EPOCH) // _MICROSECOND
    return value


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class _Unset:
    """Stands for "use sql.max_rows" - a default None can't, since None
    is itself a value to pass (no cap). The repr is what help() shows."""

    def __repr__(self) -> str:
        return "sql.max_rows"


_MAX_ROWS_UNSET: Any = _Unset()


@dataclass
class SqlTool:
    """Live mode's `sql`: a local object, not a name on the Home Assistant
    side. Calling it sends the query over the websocket, downloads the
    whole (`max_rows`-capped) result as Arrow and hands back a local
    SqlResult - so `sql(...).show()`, `.to_polars()` and anything else you
    do with the result runs in this process, with whatever you've
    installed here.

    `.max_rows` is this shell's default row cap (set it to change the
    default for every call after, or to None to remove it entirely) and
    `.tables` the recorder's tables, real and virtual, to explore the
    schema without a query
    - `.table(name)` picks one of them out by name.
    """

    client: Any
    max_rows: int | None = DEFAULT_MAX_ROWS
    tables: list[SqlTable] = field(default_factory=list)

    @classmethod
    async def connect(cls, client: Any) -> SqlTool:
        """A SqlTool with `.tables` already downloaded. Raises HaReplError
        if the server has no sql to offer (switched off in the
        integration's options, or a server too old to have the command)."""
        reply = await client.call("ha_repl_server/sql_tables")
        tables: list[SqlTable] = []
        for t in reply["tables"]:
            columns = tuple(
                SqlColumn(c["name"], c["type"], c.get("legacy", False))
                for c in t["columns"]
            )
            # class and doc are absent from a server older than this client
            described = (t["name"], columns, t.get("class", ""), t.get("doc", ""))
            if t.get("virtual"):
                tables.append(
                    SqlVirtualTable(*described, definition=t.get("definition", ""))
                )
            else:
                tables.append(SqlTable(*described))
        return cls(client, tables=tables)

    def table(self, name: str, *, legacy: bool = False) -> SqlTable:
        """The recorder table called `name`, from `.tables` - with its
        legacy columns too if `legacy`. Raises KeyError, naming the
        tables there are, if there's no such table."""
        for table in self.tables:
            if table.name == name:
                return dataclasses.replace(table, legacy=True) if legacy else table
        known = ", ".join(t.name for t in self.tables)
        raise KeyError(f"no table {name!r} - tables are: {known}")

    async def __call__(
        self,
        query: str,
        *,
        max_rows: int | None = _MAX_ROWS_UNSET,
        legacy: bool = False,
        raw: bool = False,
    ) -> SqlResult:
        """Run a single read-only SELECT against Home Assistant's Recorder
        database. At most `max_rows` rows are fetched (default: this
        object's own `.max_rows`) - pass None for no cap at all.

        A recorder column comes back as what it holds: a `_ts` or
        datetime column as a datetime in UTC, a context id as its text.
        With `raw` they are left as the database has them - seconds
        since the epoch as a float, the id's bytes.

        Legacy columns - still in a table, no longer written to - are
        downloaded but kept out of view unless the query names them or
        `legacy` is True, so a `select *` shows only the columns in use.
        Setting `.legacy = True` on the result brings them into view."""
        params: dict[str, Any] = {
            "query": query,
            "max_rows": self.max_rows if max_rows is _MAX_ROWS_UNSET else max_rows,
        }
        if raw:  # left out otherwise, which an older server needs
            params["raw"] = True
        try:
            reply = await self.client.call("ha_repl_server/sql", **params)
        except HaReplError as err:
            if err.code != "sql_error":
                raise
            raise SqlError(err.detail) from None
        return SqlResult.from_arrow(
            base64.b64decode(reply["arrow"]),
            truncated=reply["truncated"],
            tables=self._tables_of(query),
            legacy=legacy,
            hidden=self._unnamed_legacy(query),
            query=query,
        )

    def _tables_of(self, query: str) -> list[SqlTable]:
        """The tables a result of this query could be described by: the
        virtual tables it mentions - going by its words - or, if none, the
        real ones. A virtual table's columns are also those of the tables
        it joins, so without this neither could be told from the other."""
        words = set(re.findall(r"\w+", query.lower()))
        virtual = [t for t in self.tables if t.virtual and t.name.lower() in words]
        return virtual or [t for t in self.tables if not t.virtual]

    def _unnamed_legacy(self, query: str) -> frozenset[str]:
        """Names of legacy columns of the tables this query mentions that
        the query doesn't itself mention - going by its words, not a
        parse. A name that's also a column in use in another of those
        tables (`entity_id`, in a join of states and states_meta) isn't
        one of them."""
        words = set(re.findall(r"\w+", query.lower()))
        tables = [t for t in self.tables if t.name.lower() in words]
        in_use = {c.name for t in tables for c in t._columns if not c.legacy}
        legacy = {c.name for t in tables for c in t._columns if c.legacy}
        return frozenset(n for n in legacy - in_use if n.lower() not in words)

    def __repr__(self) -> str:
        return (
            f"<sql(query, max_rows=..., raw=False) - default max_rows={self.max_rows!r}; "
            "see sql.tables, sql.table(name)>"
        )
