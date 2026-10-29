---
name: ha-repl
description: Run Python against a running Home Assistant with the `ha-repl` command, to check entity state, inspect `hass` objects, query the recorder database or call services. Use when developing or debugging a Home Assistant custom component and an idea needs checking against a real instance rather than guessed from source.
---

# Home Assistant REPL

`ha-repl exec` runs one snippet of Python against a running Home Assistant and exits. Use it to
check how something really behaves before writing or changing component code.

It runs any Python inside Home Assistant. Only use it against a development instance or
devcontainer, unless the user has said the target instance is fine to change.

## Before the first call

- `ha-repl` must be on the path, or run it as `uvx --from homeassistant-repl ha-repl`.
  It needs Python 3.14. To install it for good, the user can run
  `uv tool install homeassistant-repl`; don't install it yourself.
- `HASS_SERVER` and `HASS_TOKEN` must be set in the environment or in a `.env` file in the
  working directory, or a default server set in `~/.config/ha-repl/config.toml` -
  `ha-repl servers` lists what is configured. If none of these is there, ask the user; don't
  look for a token elsewhere.
- If `ha-repl` reports that it is ignoring a `.ha-repl` directory, tell the user. Don't run
  `ha-repl trust` yourself: it approves code to run, which is the user's decision.
- The user's plugins may define extra names. `MODE` and `SERVER` are also set: `MODE`
  is `"exec"`, and `SERVER` is the name of the configured server in use, or `None`. A warning on standard error starting
  `ha-repl: plugin:` comes from those files, not from the snippet.
- `exec` needs the `ha_repl_server` custom component on the instance. If it isn't installed,
  `exec` exits with status `2`; see "Without the server component" below.
- `ha-repl` and the server component should be the same version. Prior to v1.0.0 they are likely to be breaking changes and backward compatibility between server and client is not assured.

## Running a snippet

Send the snippet on standard input, ask for JSON, and always set a timeout:

```bash
ha-repl --json exec -t 30 - <<'PY'
hass.states.get("sun.sun").state
PY
```

`--json` and `-s NAME` go before `exec`; `-t SECONDS`, `--reset` and `-f FILE` go after it.

The output is one JSON object: `stdout`, `value` (the last expression), `error` (`null`, or
`type`, `message` and `traceback`), `duration` and `truncated`. Exit status is `0` on success,
`1` if the snippet raised or timed out, `2` if Home Assistant could not be reached or the
arguments were wrong.

`await` works at the top level, and a call to an async function is awaited if the `await` is
left out.

## Names available in a snippet

| Name | What it is | Where it runs |
| ---- | ---------- | ------------- |
| `hass` | The live `HomeAssistant` object | Inside Home Assistant |
| `obj` | Entities as a tree: `obj["/mqtt/sensor/name"]`, `obj["sensor.name"]`, `obj.find(...)`, `obj.find_names(...)`, `obj.find_paths(...)`, `obj.show(path)`. Actions too: `obj["/mqtt/action/publish"](topic=..., payload=...)` calls one, `obj["/mqtt/action"].keys()` lists them | Inside Home Assistant |
| `sql` | `sql("select ...", max_rows=N)` queries the recorder and gives a local result object; `sql.tables` lists tables and columns | Local |
| `hass_api` | A `homeassistant-api` REST client | Local |

A statement that uses `hass` or `obj` is sent to Home Assistant and run there. Everything else
runs in the local `ha-repl` process.

Only plain data crosses between the two: strings, numbers, `None`, and lists, tuples, sets and
dicts of them, up to about 100,000 characters. What crosses is a copy.

- Plain local data and `sql` results can be used in a statement that runs inside Home Assistant.
  A `sql` result arrives as a list of rows. A module imported locally is imported there too.
- A variable assigned by a statement that used `hass` or `obj` comes back, and is local
  afterwards, if it holds plain data. So does what `obj.find_names()` and `obj.find_paths()`
  return: an iterator over strings, to go through once.
- Anything else - a state, an entity, a config entry - stays inside Home Assistant, and later
  statements that use that variable run there too.
- Don't use `sql` or `hass_api` in the same statement as `hass` or `obj` - it is refused. Assign
  one part on its own line first.
- Local functions, classes and dataframes don't cross. Calling a local function on a value from
  `hass` in one statement is refused; assign the plain value first, then call the function.

```bash
ha-repl --json exec -t 30 - <<'PY'
names = obj.find_names(domain="light")
{n: hass_api.get_state(entity_id=n).state for n in names}
PY
```

The first statement runs inside Home Assistant, the second locally with the names it returned.

## Finding entities

- `obj[path]` gives a sub-tree or an entity, and raises `KeyError` if there is none. `keys()`,
  `values()` and `items()` cover that level only, so `list(obj["/mqtt"].keys())` shows what is
  below a path.
- `obj.find()` goes through the whole tree in no fixed order. It takes a full or partial path or
  a regular expression, and the filters `domain`, `platform`, `area` and `label`, each a string or
  a list of strings.
- `obj.find_paths()` and `obj.find_names()` take the same arguments and give tree paths or entity
  ids instead of the objects.

## Querying the recorder

Use `sql` for what was recorded in the past: state history, events and statistics. For what an
entity's state is now, use `hass.states.get(...)` or `obj[...]`; the recorder lags behind, and
leaves out any entity excluded from recording.

A `sql` result is a local object holding the downloaded rows. `len(r)`, slices such as `r[:10]`
and looping over rows work on it. `r[0]` is one row, read by position or column name, so
`sql("select count(*) from events")[0][0]` is the number itself. Also:

- `r.column_names`, `r.rowcount`, `r.query`, and `r.truncated`, which is true when the query hit
  `max_rows`
- `r.to_dicts()` for a list of dicts, `r.project([...])` for some of the columns, `r.sample()`
  for random rows
- `r.to_polars()` or `r.to_pandas()` if that library is installed locally, `r.export_csv()` to
  write a local file

A recorder column comes back as what it holds: a `_ts` column as a `datetime` in UTC (ISO 8601
text with `--json`), `context_id_bin` and `context_parent_id_bin` as the ULID text of
`context.id`, `context_user_id_bin` as the user id. This goes by the column's name in the
result, so an expression needs the name back: `max(last_updated_ts) as last_updated_ts`.
`sql(..., raw=True)` leaves every value as the database has it.

Some recorder columns are legacy: still in the table, no longer written to, such as
`states.entity_id`, now in `states_meta`. A result keeps them out of view unless the query names them,
`sql(..., legacy=True)` is used, or `r.legacy = True` is set afterwards. A table's `columns` and
`column_names` leave them out too, unless it came from `sql.table(name, legacy=True)`.

`sql.max_rows` sets the default row limit for the rest of the snippet. `sql.table("states")`
gives one table from `sql.tables`; its `column_names` and `columns` describe it without
running a query, and `class_name` and `description` are the class and docstring it has in
`homeassistant.components.recorder.db_schema`.

### Virtual tables

The recorder keeps the name of a thing in a different table from its rows: a state's entity id,
an event's type and a statistic's id each need a join. Four virtual tables do that join, and are
queried as if they were real ones:

| Virtual table | First column | Then the columns in use of |
| ---- | ------------ | -------------------------- |
| `state_history` | `entity_id` | `states` |
| `event_history` | `event_type` | `events` |
| `statistics_history` | `statistic_id` | `statistics` |
| `statistics_short_term_history` | `statistic_id` | `statistics_short_term` |

- A virtual table is not a table in the database. It is used in a query wherever a real one could
  be: filtered, grouped, joined to other tables, real or virtual, or to itself.
- `sql.tables` lists them among the real tables, each shown as `VirtualTable(...)` with `virtual`
  set to `True`. `sql.table("state_history")` gives one: its `column_names`, and the query it
  stands for as `definition`.
- The join key itself is left out: `metadata_id` for states and statistics, `event_type_id` for
  events.
- They need server component 0.12.0 or later. If a query fails with no such table, write the
  join out instead: `states.metadata_id = states_meta.metadata_id`,
  `events.event_type_id = event_types.event_type_id`, `statistics.metadata_id = statistics_meta.id`.

### State history

`state_history` has one row per recorded state.

```bash
ha-repl --json exec -t 30 - <<'PY'
import time
since = time.time() - 24 * 3600
sql(f"""
    select state, last_updated_ts
    from state_history
    where entity_id = 'update.home_assistant_core_update' and last_updated_ts > {since}
    order by last_updated_ts desc
""", max_rows=50)
PY
```

- Times are stored as seconds since the epoch, as floats, in columns ending `_ts`:
  `last_updated_ts` on every row, `last_changed_ts` only when it differs from that, so read
  `coalesce(last_changed_ts, last_updated_ts) as last_changed_ts`. Work out a cutoff in Python,
  as above, rather than with SQL date functions, which differ between SQLite, MariaDB and
  PostgreSQL. A `where` compares the stored float; the result has a `datetime`.
- `state` is always a string, with `unavailable` and `unknown` among the values, so filter those
  out before casting to a number.
- Attributes are in `state_attributes.shared_attrs` as a JSON string, joined on `attributes_id`.
  Many state rows share one attributes row. Decode it locally with `json.loads`, as SQL JSON
  functions also differ by database.
- State history is purged after a number of days, 10 unless configured otherwise. For anything
  older, use statistics.

### Event history

`event_history` has one row per recorded event, with the time in `time_fired_ts`.

```python
sql(
    "select event_type, count(*) from event_history group by event_type order by 2 desc"
)
```

The event's data is JSON in `event_data.shared_data`, joined on `data_id`, to decode locally:

```python
sql(
    """
    select e.time_fired_ts, d.shared_data
    from event_history e
    join event_data d on d.data_id = e.data_id
    where e.event_type = 'call_service'
    order by e.time_fired_ts desc
""",
    max_rows=20,
)
```

State changes are not here: they are in `state_history`. Events are purged along with it.

### Statistics

`statistics_history` has one row an hour per statistic and is kept indefinitely.
`statistics_short_term_history` has one row every 5 minutes and is purged with state history.
`statistic_id` is an entity id for most statistics.

```python
sql(
    """
    select start_ts, mean, min, max, state, sum
    from statistics_history
    where statistic_id = 'sensor.outside_temperature'
    order by start_ts desc
""",
    max_rows=48,
)
```

`select statistic_id, unit_of_measurement from statistics_meta` lists the statistics there are,
with their units.

`start_ts` is the start of the period. Measurements fill `mean`, `min` and `max`; totals such as
energy fill `state` and `sum`, where `sum` is the running total since the statistic began, so
usage over a period is the difference between two rows.

## Getting data back

A value from inside Home Assistant comes back in `value` as data when it is plain data (strings,
numbers, lists and dicts of them), so shape the answer that way as the last expression:

```bash
ha-repl --json exec -t 30 - <<'PY'
{s.entity_id: s.state for s in hass.states.async_all("light")}
PY
```

Anything else - a state object, or a list containing one - comes back as a string of its `repr()`.

A `sql` result as the last expression comes back as data: `columns`, `rows`, `rowcount` and
`truncated`.

## State between calls

Each call is a new local process, so local variables don't carry over. Variables inside Home
Assistant do, in a named session, until it is reset or Home Assistant restarts.

- Use your own session name, `ha-repl -s <task-name> --json exec ...`, so other agents' variables
  aren't overwritten.
- `ha-repl sessions` lists sessions and their variables. `ha-repl -s <name> reset` clears one.

## Working safely

- Prefer reading. `hass.states.get(...)`, `obj[...]` and `sql(...)` are safe to repeat. Service
  calls and changes to `hass` objects take effect on the instance straight away.
- A snippet stops at the first statement that fails; statements before it have already run.
- Keep `max_rows` small while exploring (the default is 1000). Only a single `SELECT` is accepted;
  anything else raises `SqlError`.
- `hass` or `sql` access may have been switched off in the server component's options. If either
  is refused, tell the user instead of working around it.
- For an error raised inside Home Assistant, read `type` and `message`; `traceback` is formatted
  for a terminal.
- `help(thing)` inside a snippet prints a short summary of an object's methods and properties,
  for `obj`, `sql` and its results as well as `hass` objects. `help(thing, full=True)` is Python's
  own full help page.

## Looking inside an object

A state, entity, config entry or registry comes back as its `repr()`, which is often only a class
name. `show(thing)` as the last expression gives its attributes and their values instead, as text
in `value`, one attribute to a line:

```bash
ha-repl --json exec -t 30 - <<'PY'
show(hass.config_entries.async_entries("mqtt")[0])
PY
```

- Use it to find out what an object holds before writing code against it, rather than guessing
  attribute names. `help(thing)` gives the methods; `show(thing)` gives the values.
- Properties are read. Names starting with an underscore are left out unless `private=True`, and
  methods unless `methods=True`.
- Long values are cut short and say so: `... +N` for the items left out of a list or dict (30
  shown, `max_items`), `+N` for the characters left out of a string (200 shown, `max_string`).
  It is safe on `hass`, `hass.data` and the registries.
- An object held by the one shown appears as its `repr()`. `depth=2` opens those up as well.
- It only produces output as the last expression. It needs server component 0.12.0 or later.

To find out what something is without running anything - a method's signature and docstring, an
attribute's declared type - use `inspect` rather than `exec`:

```bash
ha-repl --json inspect 'hass.states.async_set'
```

The output is `doc` (`name`, `kind`, `type`, `signature`, `doc`, `value`, `owner`) and `source`
(`name`, `file`, `line`, and `url`, a GitHub link to that line at the version running), either
`null` if it can't be followed. Exit status `1` then. It needs server component 0.14.0 or later
for anything living in Home Assistant.

## Without the server component

Only read access through the standard Home Assistant API is available, and not through `exec`.
Use the library from a script instead - `connect()` gives the same `obj`, without `hass` or `sql`:

```python
import asyncio
import homeassistant_repl


async def main() -> None:
    obj = await homeassistant_repl.connect()  # reads HASS_SERVER and HASS_TOKEN
    print([(e.entity_id, e.state) for e in obj.find(domain="light")])


asyncio.run(main())
```

## More detail

Each documentation page is available as Markdown:

- Index of pages: <https://homeassistant-repl.rhizomatics.org.uk/llms.txt>
- All pages in one file: <https://homeassistant-repl.rhizomatics.org.uk/llms-full.txt>
- Exec mode: <https://homeassistant-repl.rhizomatics.org.uk/modes/exec_mode/index.md>
- What runs where, with worked examples of mixing local and Home Assistant code, and those that
  are refused: <https://homeassistant-repl.rhizomatics.org.uk/modes/live_mode/index.md>
- Object tree: <https://homeassistant-repl.rhizomatics.org.uk/obj_tree/index.md>
- SQL access: <https://homeassistant-repl.rhizomatics.org.uk/sql/index.md>
- Using the library from plain Python, ipython or Marimo:
  <https://homeassistant-repl.rhizomatics.org.uk/configuration/alternative_integration.md>
