# Exec Mode

`ha-repl exec` runs one snippet of Python against a running Home Assistant and exits. It is the non-interactive form of [live mode](live_mode.md), and its main use is giving a coding agent a way to check its ideas against a real instance: read an entity's state, query the recorder, call a service, inspect an object, then decide what to do next.

It needs the same server component as live mode - read the warnings on the [live mode](live_mode.md) page first: an agent with `exec` can run any Python inside Home Assistant. Point it at a devcontainer or development instance, not the one running your house.

## Setup

The agent needs the `ha-repl` command and two environment variables:

```bash
export HASS_SERVER=http://homeassistant.local:8123
export HASS_TOKEN=<long lived access token>
```

Both can also go in a `.env` file in the directory the agent runs from, or the server can be one named in a [`config.toml`](../configuration/client_configuration.md), where a repo's `.ha-repl/config.toml` can set the default for everything run in that repo. [Plugins](../configuration/plugins.md) are run before the snippet, so helpers defined there are available to the agent; `--no-plugins` skips them. Then tell the agent the command exists, for example in its project instructions:

```markdown
Use `ha-repl --json exec '<python>'` to run Python against the development
Home Assistant. `hass` is the live HomeAssistant object, `obj` the object tree,
`sql("select ...")` queries the recorder. See [Exec Mode](https://homeassistant-repl.rhizomatics.org.uk/exec_mode/) documentation.
```

### Agent Skill

The repo also ships an [Agent Skill](https://agentskills.io), [`skills/ha-repl`](https://github.com/rhizomatics/homeassistant-repl/tree/main/skills/ha-repl), which teaches an agent the command, its JSON output and the rules on this page, so you don't have to write the instructions yourself. Install it in the project where you develop your component.

For Claude Code, as a plugin:

```bash
claude plugin marketplace add rhizomatics/agent-plugins
claude plugin install homeassistant-repl@rhizomatics
```

For other agents that support skills, copy the `skills/ha-repl` directory into the agent's skills directory (for example `.claude/skills/` or `.agents/skills/`), or use the [skills](https://github.com/vercel-labs/skills) installer:

```bash
npx skills add rhizomatics/homeassistant-repl
```

### Documentation for Agents

Every page of this site is also published as Markdown: add `index.md` to a page's address, or use the Markdown button at the top of the page. [`llms.txt`](https://homeassistant-repl.rhizomatics.org.uk/llms.txt) lists the pages with a line on each, and [`llms-full.txt`](https://homeassistant-repl.rhizomatics.org.uk/llms-full.txt) is all of them in one file.

## Running a Snippet

Pass the code as an argument, from a file, or on standard input. Standard input avoids shell quoting problems and is the best form for anything longer than a line.

```bash
ha-repl exec 'hass.states.get("sun.sun").state'
ha-repl exec -f snippet.py
ha-repl exec - <<'PY'
r = sql("select entity_id from states_meta", max_rows=3)
{row[0]: hass.states.get(row[0]).state for row in r}
PY
```

The value of the last expression is printed, as at a Python prompt. `await` works at the top level, and a call to an async function is awaited for you if you leave the `await` out.

| Option | Effect |
| ------ | ------ |
| `--json` | Print one JSON object instead of text. Goes before `exec` |
| `-s NAME`, `--session NAME` | Use a named session inside Home Assistant (default `default`). Goes before `exec` |
| `-t SECONDS`, `--timeout SECONDS` | Give up after this long |
| `--reset` | Clear the session inside Home Assistant before running |
| `-f FILE` | Read the snippet from a file |

The exit status is `0` on success, `1` if the snippet raised or timed out, and `2` if Home Assistant could not be reached or the arguments were wrong.

## JSON Output

With `--json` the result is one object on standard output, whether or not the snippet succeeded:

```bash
ha-repl --json exec 'sql("select * from states_meta", max_rows=2)'
```

```json
{
  "stdout": "",
  "value": {
    "columns": ["metadata_id", "entity_id"],
    "rows": [[1, "zone.home"], [2, "conversation.home_assistant"]],
    "rowcount": 2,
    "truncated": true
  },
  "error": null,
  "duration": 0.004,
  "truncated": false
}
```

| Field | Content |
| ----- | ------- |
| `stdout` | Everything the snippet printed |
| `value` | The last expression, or `null` if the snippet ended in a statement |
| `error` | `null`, or an object with `type`, `message` and `traceback` |
| `duration` | Seconds the snippet took |
| `truncated` | `true` if output from inside Home Assistant was cut at its size limit |

What `value` holds depends on where the last expression ran:

- A `sql` result is its data: `columns`, `rows` (one list per row), `rowcount`, and its own `truncated`, which is `true` when the query hit `max_rows` and there were more rows to fetch.
- Any other local value that is already JSON-shaped (numbers, strings, lists, dicts) is passed through as it is. Anything else is its `repr()`.
- A value from inside Home Assistant follows the same rule when it is plain data, so `hass.states.get("sun.sun").state` gives `"below_horizon"`. Anything else - a state object, or a list containing one - is a string holding its `repr()`, so shape the answer into plain data inside the snippet.

## What Runs Where

A snippet is ordinary Python running in the `ha-repl` process, with whatever is installed there. Statements that use `hass` or `obj` are the exception: they are sent to Home Assistant and run inside it. The rules are the same as the interactive shell and are described in full under [What Runs Where](live_mode.md#what-runs-where). The points that matter most for a script:

- `sql` and `hass_api` are local. A query's result is downloaded, and everything done with it afterwards happens locally.
- Plain local data (strings, numbers, lists and dicts of them) and `sql` results can be used in a statement that runs inside Home Assistant. A `sql` result arrives there as a list of rows.
- A variable assigned by a statement that used `hass` or `obj` comes back, and is local afterwards, if it holds plain data. Anything else stays inside Home Assistant, and later statements that use it run there too.
- Don't use `sql` or `hass_api` in the same statement as `hass` or `obj` - it is refused. Assign one part on its own line first.

[Mixing Both Sides](live_mode.md#mixing-both-sides) has the details and worked examples.

## State Between Calls

Each `exec` call starts a new local Python process, so local variables and imports do not carry over. Variables inside Home Assistant do: they live in the named session until it is reset or Home Assistant restarts.

```bash
ha-repl exec 'entry = hass.config_entries.async_entries("sun")[0]'
ha-repl exec 'entry.state'          # still there
ha-repl sessions                    # list sessions and their variables
ha-repl reset                       # clear the default session
```

Give each agent, or each task, its own `--session` name if several run against the same instance, so they don't overwrite each other's variables.

## Errors

Without `--json`, a traceback goes to standard error and the exit status is `1`. With `--json`, `error` is filled in and `stdout` still holds whatever was printed before the failure:

```json
{
  "stdout": "",
  "value": null,
  "error": {
    "type": "ZeroDivisionError",
    "message": "division by zero",
    "traceback": "Traceback (most recent call last):\n  File \"/ha_repl/api_cell_1\", line 1, in <module>\n    x = 1/0\n        ~^~\nZeroDivisionError: division by zero\n"
  },
  "duration": 0.001,
  "truncated": false
}
```

A snippet stops at the first statement that fails. Statements before it have already run, including any that changed something inside Home Assistant.

For an error raised inside Home Assistant, `type` and `message` are plain, but `traceback` is formatted for a terminal with box-drawing characters. Rely on `type` and `message` when parsing.

A rejected query raises `SqlError`. Only a single `SELECT` statement is accepted.

## Tips for Agents

- Use `--json`, and send the snippet on standard input.
- Always set `-t`. A snippet that waits on something that never happens otherwise blocks forever.
- Keep `max_rows` small while exploring. The default is 1000 rows; `sql.tables` lists the tables and their columns without running a query.
- Prefer reading to writing. `hass.states.get(...)`, `obj[...]` and `sql(...)` are safe to repeat; service calls and direct changes to `hass` objects take effect immediately on the instance.
- `show(thing)` as the last expression gives an object's attributes and their values, cut down to a readable size - see [Looking Inside Objects](live_mode.md#looking-inside-objects). With `--json` it comes back in `value` as text.
- `help(thing)` prints a summary of an object's methods and properties, which is often quicker than reading the source. `help(thing, full=True)` gives Python's own full help page instead.
- `ha-repl --json inspect EXPR` describes what an expression names - kind, type, signature and docstring - without running it, and gives where its source is, with a GitHub link at the version running. It's decided where to look as `exec` would, plugins included. `--source` adds the location to the plain text output too. Exit status `1` if it can't be followed without running something.
