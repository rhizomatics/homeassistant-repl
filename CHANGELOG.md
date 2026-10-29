# What's New

!!! note
    For live mode, generally the client and server component have to be on the same version. Different versions *might* work
    with each other for point releases

# 0.14.0

This release brings auto-complete for code and the object tree, integrated help/source-code lookup, much improved CLI history and lots more.

## ✨ Enhancements
### Shell
- A whole statement or block is edited and kept in history as one entry, in live and API modes alike
  - Up brings back a complete `for` loop, `def` or `with` block, and any line of it can be changed before running it again
  - Enter runs the code once it's complete, and the cursor is at the end. Otherwise it starts a new line, indented to match, one level deeper after a `:` and one shallower after `return`, `pass`, `break`, `continue` or `raise`
  - A block is ended by an empty line, as before. Alt-Enter (Esc then Enter) runs what's there straight away
  - Entries already in the history file from earlier versions are still there, a line each

### Live Mode
- Ctrl-C while a command runs stops it, rather than the shell
  - Code running inside Home Assistant is stopped there, and its traceback shows where it had got to. A second Ctrl-C stops waiting for it
  - Local code stuck in a blocking call is interrupted by a second Ctrl-C
  - Also in API mode, for its local code
  - Stopping code inside Home Assistant needs the server component at this version

### Fixes
- A connection lost while the shell is open - Home Assistant restarting, say - is reported as `Connection closed`, rather than a traceback from the HTTP library, and leaving the shell still works

### Live Server
- If `nanoarrow` can't be installed or loaded, only `sql` is switched off, with the reason in the log and the shell, rather than the whole integration failing to load
  - `nanoarrow` and `sqlparse` are installed when the integration starts, no longer as its manifest requirements
- Completion's limits, 500 items and 500 milliseconds an answer by default, are options in a **Limits** section of the *Configure* form
- New **Stop running code** button on the integration's device, stopping whatever is running in every live session
- New `ha_repl_server/cancel` websocket command, which Ctrl-C in the shell sends
- Every command is written to the Home Assistant log at `warning`, so it shows by default, with the user who sent it
  - On a logger of its own, `custom_components.ha_repl_server.commands`, to quieten without hiding other warnings
  - The code run, then its outcome: the value or error, how much output, and how long it took
  - Each SQL query, and its row count and time, or its error
  - Each stop and reset

### Auto-Completion
- Tab completes names, attributes and keys in both shells, and a menu opens by itself after a `.`, in a key, and after two letters of a name ([RFC 0002](https://homeassistant-repl.rhizomatics.org.uk/developer/design/rfcs/0002-auto-completed/), steps 2 and 3)
  - Nothing is run to complete: no property, call, or lookup in anything but a plain `dict`, `list` or `tuple`, or the object tree - return and property annotations are followed instead
  - `obj["` completes paths, and entity ids at the root, and `obj.find("` paths
  - In live mode, a statement using `hass`, `obj` or a variable living in Home Assistant is completed by Home Assistant, from the session itself, even while code runs there. Needs the server component at this version
  - Local code the walk can't follow, such as a loop variable, is completed by [jedi](https://jedi.readthedocs.io), now a dependency
  - What's typed matches anywhere in a name, in any case - names starting with it first
  - Inside a call, its signature is shown under the prompt with the argument being typed in bold, and its parameters not yet given complete as `name=` (step 5)

### Help
- IPython style help in both shells (step 6)
  - `?` on its own: what the shell offers in its mode, and its keys
  - `thing?`: `help(thing)`, run where `thing` lives, so `hass?` is answered by Home Assistant. Never calls anything to get there
  - `thing??`: file and line of its source, and a link to that line on GitHub at the version running - Home Assistant's release or beta tag, this project's, or Python's
  - For an attribute its class declares or documents, `thing?` gives its declared type and what the class's docstring says about it, rather than help on the value's type - `sql.tables?` no longer describes `list`
  - Ctrl-Space (or Ctrl-Shift-Space) describes the name under the cursor, or the call it's in - signature and docstring - above the prompt, without running anything
- New `ha-repl inspect EXPR`, the same description for an agent, routed as `exec` is: `--json` gives it with the source location, `--source` adds the location to plain text
- New `ha_repl_server/complete` websocket command, for completions, signatures (`kind: "signature"`) and source locations (`kind: "source"`), and `ha_repl_server/info` lists the commands a server has

### Object Tree
- `obj["` completes paths, and entity ids at the root, in IPython, Jupyter and Marimo, for `obj` from `connect()` as well as in live mode (step 1 of [RFC 0002](https://homeassistant-repl.rhizomatics.org.uk/developer/design/rfcs/0002-auto-completed/))
- A registry entry's keys complete as attributes there too: `entity.registry_entry.uni<TAB>`

### Docs
- [RFC 0002](https://homeassistant-repl.rhizomatics.org.uk/developer/design/rfcs/0002-auto-completed/) has the results of its step 0 spike, and where `ha-repl` would still differ from the Debug console once it's done
- New [Debug Console](https://homeassistant-repl.rhizomatics.org.uk/debug_console/) page, comparing `ha-repl` with the Debug console that Home Assistant 2026.11 adds

# 0.13.0
## ✨ Enhancements
### SQL
- A result column of a Recorder table comes back as what it holds, going by how `db_schema` declares it
  - A `_ts` column (`TIMESTAMP_TYPE`) and a `DATETIME_TYPE` column as a `datetime` in UTC, in place of seconds since the epoch or text
  - `context_id_bin` and `context_parent_id_bin` as the ULID text of `context.id`, and `context_user_id_bin` as the user id, in place of bytes
  - `sql("...", raw=True)` leaves every value as the database has it
  - Needs both the client and the server component at this version
- A result has the `query` it came from

### Object Tree
- Actions (services) are in the tree, in an `action` level beside each integration's entity domains - `obj["/mqtt/action/publish"]`
  - Call one to run it, with its data as keyword arguments and `target` for what it acts on: `obj["/light/action/turn_on"](target={"entity_id": "light.shed"}, brightness=128)`
  - What an action that answers gives back is returned
  - `name`, `description`, `fields` and `supports_response` describe it, as does `obj.show(path)`, with the names and descriptions the frontend shows for the action and each of its fields
  - The find methods go through actions as well as entities: `obj.find_names(domain="action")` gives every action, and a search with no `domain` now gives both
  - `object_type="entity"` or `object_type="action"` keeps a find to the one kind. `"device"` is accepted, for when devices are in the tree
  - In live, exec and API mode alike. An integration with actions and no entities is now in the tree
  - Live mode needs the server component at this version

### Live Mode
- New `rebind()`, to put back any of the shell's own names - `hass`, `obj`, `sql`, `hass_api`, `show`, `help`, `MODE`, `SERVER` - that has been assigned over, and run the plugins again
  - `rebind(plugins=False)` leaves the plugins alone
  - Also in `ha-repl exec` and API mode

### Live Server
- `hass` and `sql` can each be given a time limit, in minutes, from the integration's *Configure* form, after which that one is switched off again
  - Left empty, it stays on, as before
  - The limit survives a restart of Home Assistant, and one that ran out while it was stopped is applied when it starts
  - *Configure* shows the minutes left on a running limit, to change or clear

# 0.12.0
## ✨ Enhancements
### SQL
- New virtual tables, to query as if they were real ones, in place of the join almost every query on these tables needs
  - `state_history` is `states` with each row's `entity_id` as the first column
  - `event_history` is `events` with each row's `event_type`
  - `statistics_history` and `statistics_short_term_history` are `statistics` and `statistics_short_term` with each row's `statistic_id`
  - `sql("select * from state_history where entity_id = 'sensor.outside_temperature'")`
  - They leave out the legacy columns and the join key
  - Nothing is created in the Home Assistant database - a virtual table is added to the query as a `with` clause when the query mentions it
  - `sql.tables` lists them, each as a `VirtualTable` object rather than a `Table`, with a `virtual` flag and the query it stands for as its `definition`
  - Needs both the client and the server component at this version



### Live Mode
- New `show(thing)`, to look inside an object that otherwise prints as little more than its class name: its attributes and their values, in colour, one to a line
  - `show(hass.config)`, `show(hass.states.get("sun.sun"))`
  - Names starting with an underscore are left out unless `private=True`, and methods unless `methods=True`
  - Anything enormous is cut short, with a count of what was left out, so `show(hass.data)` is safe - `max_items`, `max_string` and `depth` set how much is shown
  - Nothing new is installed in Home Assistant: the server sends a description of the object, and the shell draws it
  - Works on local objects too, and in `ha-repl exec`, where it comes back as text

## 📚 Documentation
- The agent skill and *Examples* cover querying state history, events and long term statistics, and looking inside objects with `show()`

# 0.11.1
## 🐛 Fixes
### Live Mode
- A name used only in a type annotation no longer decides which side a statement runs on, and no longer stops it running inside Home Assistant because it "only exists in the local session"
- A bare declaration, such as `hass: HomeAssistant`, is no longer treated as an assignment

## ✨ Enhancements
### Plugins
- `homeassistant_repl.plugin` declares `MODE`, `SERVER`, `hass`, `obj`, `sql` and `hass_api` for linters and type checkers, to import under `TYPE_CHECKING`
- The package now carries a `py.typed` marker

## 📚 Documentation
- *Plugins* covers importing a component's own classes, and type checking a plugin
- A link is given to the example [Supernotify ha-repl plugin code](https://github.com/rhizomatics/supernotify/blob/main/.ha-repl/plugins/10-supernotify-internals.py)

# 0.11.0
## ✨ Enhancements
### Configuration
- Implementation of [RFC-0001: Persistent Config](https://homeassistant-repl.rhizomatics.org.uk/developer/design/rfcs/0001-persistent-config/)
- Servers can be named in `~/.config/ha-repl/config.toml` and chosen with `--server NAME` or `HASS_SERVER=NAME`, with `default` picking one when neither is given. `--server` still takes a URL
  - A server's token comes from the file, from a command (`token_command`, for a secrets manager) or from a named environment variable (`token_env`)
  - `HASS_TOKEN` is only used with a URL, never with a named server
  - `ha-repl servers` lists the servers configured, without their tokens
- Python files in `~/.config/ha-repl/plugins/` are run at the start of every session, including `ha-repl exec`, for helper functions and bookmarked expressions. `--no-plugins` skips them
  - `MODE` (`"live"`, `"exec"` or `"api"`) and `SERVER` (the configured server's name) are set, so one file can adapt to where it is running
- A repo can carry its own `.ha-repl/` directory, layered over the one in the home directory. It is ignored until allowed with `ha-repl trust`, and again whenever its contents change
- `session`, `ttl` and `auto_await` can be given defaults in `config.toml`. `--auto-await` is new, to turn it back on for one run
- `homeassistant_repl.connect()` accepts a server name, e.g. `connect("house")`
- See the new *Configuration* and *Plugins* pages

### API Mode
- `ApiEntity` is closer to the real `Entity`, so more code works unchanged in both `api` and `live` mode
  - `registry` is renamed `registry_entry`, and can be read by attribute as well as by key: `e.registry_entry.unique_id`
  - `platform` is still the integration's name as a string, and also has `platform_name` and `domain`
  - New properties with the real `Entity` names: `unique_id`, `available`, `enabled`, `device_class`, `unit_of_measurement`, `icon`, `entity_picture`, `supported_features`, `assumed_state`, `attribution`, `entity_category`, `has_entity_name` and `translation_key`
  - The same applies to `obj.mode("api")` in live mode, so the server component needs updating too

## 📚 Documentation
- Reorganized and improved the configuration and installation

# 0.10.0
## ✨ Enhancements
### Help
- `help()` on something local - `obj` in API mode, `sql` and its results, tables and rows - now gives a short summary of what it is for, its methods, properties and attributes, as it already did for objects inside Home Assistant. Special methods, inherited members, data descriptors and the method resolution order are left out
- `help(thing, full=True)` gives Python's own full help page, on either side
- `help(obj)` explains how to use the object tree
- Type annotations in the summary are no longer shown in quotes
### SQL
- `sql.table("states")` returns the `Table` object for a single table, by name
- `Table` objects have `class_name` and `description`, the name and docstring of the class the Recorder maps to the table, e.g. `States` and `State change history.`
- Legacy columns - those Home Assistant marks `UNUSED_LEGACY_COLUMN`, still in a table but no longer written to - are now told apart
  - `Column` objects have a `legacy` flag
  - `columns` and `column_names` on a `Table` leave them out, unless the table came from `sql.table("states", legacy=True)`
  - A query result keeps them out of view - in `show()`, rows, dataframes and exports - unless the query names them or `sql(..., legacy=True)` is used. Setting `legacy = True` on the result afterwards brings them into view without a new query
- `show()` on a result no longer cuts off at 6 columns by default, so it shows the same columns as a row from `result[0]`. Use `max_cols` to cap them
- Indexing a result with a row number, e.g. `sql("select count(*) from events")[0]`, returns a `Row` object - its values, readable by position or column name, with its column names and `Table`
- `columns` and `column_names` on a `Table` object are now properties, not methods
## 📚 Documentation
- Added help on using `ha-repl` from the **Studio Code Server** and **Advanced SSH** Home Assistant apps (aka addons), and using `ttyd`
- Lots more examples of how to use all the builtins

# 0.9.0
## Live Server
- No longer needs `homeassistant-api`: `hass_api` is the shell's own local REST client, and is no longer also bound inside Home Assistant
## Live Shell
- A variable assigned inside Home Assistant now comes back, and is local afterwards, when it holds plain data - so `names = obj.find_names(...)` can be followed by local code using `names`. See *Mixing Both Sides* in the live mode docs
- A statement using `sql` or `hass_api` together with `hass` or `obj` is refused with an explanation, rather than failing inside Home Assistant
- `ha-repl --json exec` reports a plain-data value from inside Home Assistant as data, not as its printed form

# 0.8.0
## Live Server
- No longer uses `rich`, so installing no longer upgrades the much older `rich` bundled with Home Assistant 2026.10 - which could fail setup with `cannot import name 'SyntaxPosition' from 'rich.syntax'` until a second restart
  - Values and errors are now sent to the client as data, and rendered there
  - Needs a matching client: an older `ha-repl` still works, but shows plain values and tracebacks
## Live Shell
- Fix for a class whose body calls something (e.g. a `dataclasses.field()` default) failing with `'await' outside function`
- New gallery of examples of blending local and server side objects, the gallery also acts as a set of automated tests

# 0.7.0
## Live Shell
- The live mode is now local first, so arbitrary local imports can be made.
  - `obj` and `hass` are the only fake objects, redirecting their inputs and outputs to the real equivalents inside the live server
  - Both these objects are limited to accepting local primitive values only as arguments, and can't be combined in a single statement
## SQL
- Results object has new features:
  - It can now be sliced like a list, e.g. `[:-10]` for last 10 rows, `[:10]` for first 10
  - `project()` method which takes a list of columns and produces same table with just those columns
  - `sample()` returns a random sample of rows from the table, defaulting to 20, overridable with `count=` 
  -  `export_csv()` method, with same keyword arguments as `csv.writer`
  - `len()` can be called on result set to get size, subject to row limits
  - The results can be iterated over with a list of values per row
    `for e in sql('select * from events'):
        print(e[0],e[5])`
  - The Apache Arrow / Feather data is available as `arrow()` using the PyCapsule interface for max compatibility
  - Internally the data is more efficiently stored, as single native Arrow object
- Queries all executed under `read_only` session scope so no commits expected (this is not a security measure)
## Live Server
- Config switches now to optionally switch off `sql` and/or `hass` access


# 0.6.0
- Live Mode now has direct SQL query access
 - New `sql` global var, with `sql.tables` to show choice of tables
   - `sql('select data_id,shared_data from event_data')`
   - Tabular `show()` on results using `rich`
   - Zero-copy export to polars or pandas dataframes (optional dependencies)
 - Goes through Home Assistant's *Recorder* integration for maximum compatibility with Home Assistant and with any of the DB types used (e.g. sqlite, MySQL, PostgreSQL)
 - Uses `arrow` format for client/server communication for efficiency and compatibility. 
   - Direct integration into `polars` or `pandas` without relying on these libs
   - `nanofeather` used for lightest Arrow implementation
- `custom` mode is now `live` mode
- Version of REPL client shown at start up, and of server if in live mode
- `help()` response now uses `rich` formatting
- Fix for `rich` lazy import breaking connection

## 0.5.1
- Re lock `uv` for release, no code change

## 0.5.0
- `obj` will take a plain entity name now as well as a path
  - For example, `obj("binary_sensor.kitchen_pir")`
- `obj.find`,`obj.find_names` and `obj.find_paths` now take regular expressions
  - For example, `obj.find_names("/mqtt/binary_sensor/kitchen.*")` or `obj.find_names("/mqtt/binary_sensor/.*_leak")` or just `obj.find_names(".*_leak")`
- Renamed `--url` to `--server` on `ha-repl` for consistency with environment variable names

## 0.4.0
- `live` mode now offers switch back to `api` mode when calling `obj`
  - Use `obj.mode("api")` or `obj.mode("live")` to control session
- To minimize confusion, the [homeassistant-api](https://github.com/HomeAssistant-API/HomeAssistantAPI) integration now uses the `hass_api` variable

## 0.3.0
- Integrated [homeassistant-api](https://github.com/HomeAssistant-API/HomeAssistantAPI) client object as `api` variable for pure REST/WS API access
  - `api.get_state(entity_id="binary_sensor.kitchen_pir")`
  - `api` has its own `AsyncEntity` class which differs from the `ApiEntity` used by the core REPL
     - reason is that `ApiEntity` is designed to offer a subset of the real entity and allow easier prototyping of custom component code whereas `AsyncEntity` is the `homeassistant-api` package way of working purely with API data
- Auto await will do the `await` for you with coroutines if you forget.
  - Can be switched off completely with `--no-auto-await`
  - Can be one-off switched off by doing an `unawait` to get raw coroutine
- Replaced use of `websockets` with `niquests[ws]`

## 0.2.0
- Renamed to `Home Assistant REPL` and cli tool to `ha_repl` to make it clearer what it does and avoid confusion in package name space or terminal with other dev shells.

## 0.1.2
- Simpler `connect` method to use the API and `obj` from any Python async code
- Environment variables can be sourced from an `.env` file

## 0.1.1
- Changed `api` mode prompt to standard Python `>>>`
- Improved docs and tests
- Default Home Assistant URL is now `http://homeassistant.local:8123`

## 0.1.0

Initial working shell, useful in both `api` (API use only) and `custom` (custom server component) modes.

See [Roadmap](./developer/design/roadmap.md) for where this might go next.