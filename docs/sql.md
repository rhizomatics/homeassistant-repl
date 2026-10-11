# SQL Access

Home Assistant has an internal SQL database front-ended by the [Recorder](https://www.home-assistant.io/integrations/recorder/) integration. It holds, history, activity, long term statistics and more. Underneath it can by Sqlite (most common), MariaDB, MySQL or PostgreSQL.

When in `live` mode, the `ha-repl` exposes an `sql` variable, which allows access to this database, via the Recorder interface. 

`sql` and its results are local objects. A query is sent to Home Assistant, the rows come back as one download, and everything after that - `show()`, slicing, dataframes, CSV export - works on that local copy, with whatever libraries you have installed locally. Nothing extra needs installing in Home Assistant.

!!! info 
    Data is serialized as Apache Arrow using the `nanofeather` library - this provides column-oriented data for good compression, zero-copy reuse into dataframes and good compatibility with popular data libraries like `polars` and `pandas`.

### `sql` Object

| Operation | Description |
| --------- | ----------- |
| `sql("<query>")` | Send a query to the database and wait for results |
| `sql("<query>", legacy=True)` | The same, with [legacy columns](#legacy-columns) in view |
| `sql("<query>", raw=True)` | The same, with every value [as the database has it](#column-values) |
| `sql.max_rows` | Read/write property to set the default row limit for results in this session |
| `sql.table("<name>")` | Returns the `Table` object that describes the named table and its contents, or raises `KeyError` if there is no such table |
| `sql.table("<name>", legacy=True)` | The same, with the table's [legacy columns](#legacy-columns) included |
| `sql.tables` | Returns a list of `Table` objects that describe each table and its contents, with a `VirtualTable` for each [virtual table](#virtual-tables) |

For example this will show the size of the `statistics` table.

```python
>>> results=sql('select * from event_types')
>>> sql('select count(*) from statistics').show()
```

![select count](assets/screenshots/select_count.png)

### Result Objects

On the result object, `show()` will display data in tabular form at the command, using the `rich` library's table support. 

The result will have its rows automatically limited, so it doesn't lock up the console - use `max_rows` to override this. The columns shown are the same ones a row of the result has, so `show()` and `[0]` always agree; use `max_cols` to cap how many, or `columns` to set the list of columns specifically.

A single row can be picked out by position, so `[0]` for the first row or `[-1]` for the last - see [Row Objects](#row-objects). The results can also be sliced like a Python list, so `[:10]` for the first 10 rows or `[-10:]` for the last 10. Slices share the downloaded data rather than copying it. For anything more advanced - a step, filtering, sorting, joins - turn the result into a dataframe with `to_polars()`, which also uses the downloaded Arrow data directly. You can also call `len()` on the results to get the number of rows.

| Operation.     | Description        |
| -------------- | ------------------ |
| `rowcount`     | Size of result set, may not be size of table due to row limits |
| `truncated`    | `True` if results were truncated due to row limits |
| `query`        | The query this is the result of, as it was given to `sql` |
| `legacy`       | Read/write flag, set to `True` to bring [legacy columns](#legacy-columns) into view |
| `table`        | The `Table` object describing this table, or `None` if the columns don't identify a single table |
| `column_names` | List of the column names in the table, in order |
| `show()`       | Dump the table data to the console |
| `project()`    | Create a new result object with a limited set of columns based on this one |
| `sample()`     | Randomly sample selected quantity of rows out of the result set |
| `export_csv()` | Write the results to a local CSV file, by default named after the table (`result.csv` if there isn't one) |
| `arrow()`      | The result as an Arrow array, for Arrow-aware libraries, e.g. `polars.DataFrame(r.arrow())` or `pyarrow.table(r.arrow())` |
| `arrow_ipc()`  | The result as Arrow IPC stream bytes, e.g. to save to a file |
| `to_json()`    | The results as a JSON string of `columns`, `rows`, `rowcount` and `truncated` |
| `to_dicts()`   | Extract a `list` of `dict` objects from the results |
| `to_pandas()`  | Turn the results into a *pandas* dataframe, if pandas installed |
| `to_polars()`  | Turn the results into a *polars* dataframe, if polars installed |

### Column Values

A column of a Recorder table comes back as what it holds, not as how the database stores it. This goes by how Home Assistant declares the column in `db_schema`.

| Declared in `db_schema` as | Columns | Stored as | In a result |
| -------------------------- | ------- | --------- | ----------- |
| `TIMESTAMP_TYPE` | Those ending `_ts`, such as `last_updated_ts`, `time_fired_ts`, `start_ts` | Seconds since the epoch, as a float | A `datetime` in UTC |
| `DATETIME_TYPE` | `start`, `end` and `created` of `recorder_runs`, `start` of `statistics_runs`, `changed` of `schema_changes` | A date and time, as text in SQLite | A `datetime` in UTC |
| `CONTEXT_BINARY_TYPE` | `context_id_bin`, `context_parent_id_bin` | 16 bytes | The ULID as text, as `context.id` has it |
| `CONTEXT_BINARY_TYPE` | `context_user_id_bin` | 16 bytes | The user's id as text, as `context.user_id` has it |
| `Boolean` | `has_sum`, `closed_incorrect` | `0` or `1` in SQLite | `True` or `False` |

The times are Arrow timestamps, so they are also datetimes in a `polars` or `pandas` dataframe, and ISO 8601 text in `to_json()`.

Pass `raw=True` for the values as the database has them instead.

```python
>>> sql("select entity_id, last_updated_ts, context_id_bin from state_history")[0]
>>> sql("select entity_id, last_updated_ts, context_id_bin from state_history", raw=True)[0]
```

- A column is recognised by its name in the result, so `max(last_updated_ts)` is a plain number unless it is given the name back, as in `max(last_updated_ts) as last_updated_ts`
- This only changes what comes back. Inside the query the columns are still as stored, so a `where` on a `_ts` column compares it to seconds since the epoch
- `state` is text in `states` and a number in `statistics`, so it is left as it comes

### Row Objects

Indexing a result with a row number gives a `Row` object. It reads like a list of the row's values, in column order, and can also be indexed by column name.

| Operation      | Description        |
| -------------- | ------------------ |
| `values`       | List of the values in the row, in column order |
| `column_names` | List of the column names, in the same order |
| `table`        | The `Table` object of the result the row came from, or `None` if it had none |
| `to_dict()`    | The row as a `dict` of column name to value |

```python
>>> sql("select count(*) from events")[0][0]
>>> row = sql("select * from states_meta")[0]
>>> row["entity_id"]
>>> {col: sql(f"select count(distinct {col}) from events")[0][0] for col in sql.table("events").column_names}
```

### Table Objects

A `Table` object describes one table, using Home Assistant's own definition of it in `homeassistant.components.recorder.db_schema`.

| Operation      | Description        |
| -------------- | ------------------ |
| `name`         | The table's name in the database, e.g. `states` |
| `class_name`   | The name of the class the Recorder uses for this table, e.g. `States`, to look up in `db_schema`. Empty for a virtual table |
| `description`  | That class's docstring, e.g. `State change history.` |
| `columns`      | List of `Column` objects, each with a `name`, `type` and `legacy` flag |
| `column_names` | List of the column names, in order |
| `virtual`      | `True` if this is a [virtual table](#virtual-tables) rather than a real table in the database |

Both leave out the [legacy columns](#legacy-columns), unless the table came from `sql.table("<name>", legacy=True)`.

### Virtual Tables

A virtual table is a ready-made join, to query as if it were a table. It saves writing out a join that almost every query on that table needs, because the Recorder keeps the name of a thing - an entity id, an event type, a statistic id - in a different table from its rows.

Each virtual table has that name as its first column, then the columns of its table that are in use, without the [legacy columns](#legacy-columns) or the key the two tables are joined on.

| Virtual table | What it holds |
| ---- | ------------- |
| `state_history` | One row per recorded state, with its `entity_id`: `states` joined to `states_meta` |
| `event_history` | One row per recorded event, with its `event_type`: `events` joined to `event_types` |
| `statistics_history` | One row an hour per long term statistic, with its `statistic_id`: `statistics` joined to `statistics_meta` |
| `statistics_short_term_history` | One row every 5 minutes per statistic, with its `statistic_id`: `statistics_short_term` joined to `statistics_meta` |

```python
>>> sql("select * from state_history where entity_id = 'sensor.outside_temperature'").show()
>>> sql("select entity_id, count(*) from state_history group by entity_id").show()
>>> sql("select event_type, count(*) from event_history group by event_type").show()
>>> sql("select start_ts, mean from statistics_history where statistic_id = 'sensor.outside_temperature'").show()
>>> sql.table("state_history").column_names
```

A virtual table can be used wherever a real one can in a query - filtered, grouped, joined to other tables, real or virtual, or to itself - and the database does all of that work, as it would for the join written out.

`sql.tables` lists them among the real tables. Each is a `VirtualTable` object, which shows as `VirtualTable(...)` rather than `Table(...)`, so the two can't be mistaken for each other. It has everything a `Table` has, with `virtual` set to `True`, an empty `class_name`, and one thing more:

| Operation      | Description        |
| -------------- | ------------------ |
| `definition`   | The query the virtual table stands for |

```python
>>> [t.name for t in sql.tables if t.virtual]
>>> sql.table("state_history").definition
```

!!! info
    Nothing is created in the Home Assistant database. When a query mentions a virtual table by name, its definition is added to the query as a common table expression (a `with` clause) before it is run. If the query has a `with` clause of its own, the definition joins that one.

### Legacy Columns

Some Recorder tables still have columns that Home Assistant no longer writes to, because the data moved elsewhere - `entity_id` in `states`, for example, is now in `states_meta`. Home Assistant marks these in `db_schema` as `UNUSED_LEGACY_COLUMN`, and they are empty for anything recorded recently.

They are kept out of view by default, both in a `Table` and in a query's result. A query still downloads them, but the result leaves them out of everything it does - `show()`, rows, `column_names`, dataframes, exports - so `select * from states` shows only the columns in use.

A legacy column is in view when:

- the query names it
- the query is made with `legacy=True`
- `legacy` is set to `True` on the result afterwards, which needs no new query

```python
>>> r = sql("select * from states")                 # columns in use
>>> r.legacy = True                                 # now every column
>>> sql("select * from states", legacy=True)        # every column from the start
>>> sql("select state_id, entity_id from states")   # named, so in view
>>> sql.table("states").column_names               # columns in use
>>> sql.table("states", legacy=True).column_names  # every column
```

A query is checked by its words rather than parsed, so a legacy column mentioned anywhere in it, such as in a `where`, is in view.

### Dataframe Portability

Since internally `pyarrow` is used for moving and holding data, and the `arrow()` method returns a `pycapsule` compliant interface, the data can be used in other tools that accept Arrow. 

In this example the dataframe-neutral [narwhals](https://narwhals-dev.github.io/narwhals/) library is used to construct a dataframe, which can then have implementation neutral manipulation applied, or re-exported to another dataframe type.

```python
import narwhals as nw
import polars as pl
>>> df=nw.from_arrow(sql('select * from events',backend=pl).arrow())
```

### More Help

Use `help(sql)`, or call `help(..)` on a result, row or table object, to get reminded of the API. It gives a short summary of the methods, properties and attributes; add `full=True` for Python's own full help page.
