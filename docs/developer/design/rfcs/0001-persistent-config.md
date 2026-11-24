# RFC 0001: Persistent Configuration and Plugins

| | |
| --- | --- |
| Status | Closed: implemented in 0.11.0 - see [Configuration](../../../configuration/client_configuration.md) and [Plugins](../../../configuration/plugins.md) |
| Date | 2026-10-07 |
| Affects | `ha-repl` command, `homeassistant_repl.connect()`. No change to the server component |

## Summary

Add a configuration directory, `~/.config/ha-repl/`, holding two things:

- `config.toml`, which names the Home Assistant servers you work with, so one can be picked by name
- `plugins/`, a directory of plain Python files run at the start of every session, for bookmarked expressions, helper functions and classes

A repo can carry the same layout in a `.ha-repl/` directory, which is layered over the one in the home directory.

Both mechanisms are borrowed: named servers from kubectl contexts and AWS profiles, and the startup directory from IPython. Everything that works today (`--server`, `--token`, `HASS_SERVER`, `HASS_TOKEN`, `.env`) keeps working and keeps its precedence.

## Motivation

Three things are awkward at present:

1. **More than one Home Assistant.** A component developer usually has a devcontainer instance, perhaps a test instance, and the one running the house. Switching means re-exporting two environment variables or swapping `.env` files, and it is easy to end up with the server from one and the token from another.
2. **Nothing persists.** A useful expression such as `hass.data["entity_components"]["sensor"]`, or a five line helper, has to be found in history or retyped in every session. Variables inside Home Assistant last until it restarts; local ones don't outlive the process.
3. **Work spans repos.** A developer with several components wants the same servers and helpers in each, plus a few things specific to one component. A `.env` in each repo doesn't share anything.

The configuration should also be easy to keep in a dotfiles manager such as chezmoi, which means plain text files in a predictable place, with a way to keep tokens out of them.

## Design

### Where configuration lives

```
~/.config/ha-repl/              # $XDG_CONFIG_HOME/ha-repl if that is set
  config.toml
  plugins/
    10-helpers.py
    20-bookmarks.py

<repo>/.ha-repl/                # nearest one walking up from the working directory
  config.toml
  plugins/
    50-this-component.py
```

`~/.config` is used on every platform, including macOS, as `git`, `gh` and `uv` do. It is the location dotfiles tools expect.

The repo directory is found by walking up from the working directory and stopping at the first `.ha-repl/`, or at the root of the git repository if none is found.

Both directories are optional, and so is each file within them.

### `config.toml`

```toml
default = "dev"          # server used when none is named

session = "default"      # same as --session
ttl = 30                 # same as --ttl
auto_await = true        # false is the same as --no-auto-await

[servers.dev]
url = "http://localhost:8123"
token = "eyJ..."

[servers.house]
url = "https://ha.example.org"
token_command = "op read op://Private/ha-house/token"

[servers.test]
url = "http://ha-test.local:8123"
token_env = "HA_TEST_TOKEN"
```

A server needs a `url` and one of:

| Key | Token comes from |
| --- | ---------------- |
| `token` | The file itself |
| `token_command` | The standard output of a command, run without a shell |
| `token_env` | A named environment variable |

`token_command` is what lets the file sit in a public dotfiles repo. A plain `token` suits a chezmoi template that fills it in from a secrets manager.

The repo file is merged over the home file: top-level keys are replaced, and `servers` is merged by name and then key by key. A typical repo file is one line:

```toml
default = "dev"
```

### Choosing a server

`--server` and `HASS_SERVER` accept either a configured name or a URL. A value with no `://` in it is looked up as a name, and it is an error if no server has that name.

```bash
ha-repl --server house live
HASS_SERVER=test ha-repl exec 'hass.config.version'
ha-repl live                        # uses `default`
ha-repl --server http://10.0.0.5:8123 --token ... live     # unchanged
```

The interactive banner shows the name as well as the address, and `ha-repl servers` lists what is configured and which is the default, without printing tokens.

### Precedence

For both the server and the token, the first of these that gives a value wins:

1. `--server` / `--token`
2. `HASS_SERVER` / `HASS_TOKEN` in the environment
3. `HASS_SERVER` / `HASS_TOKEN` in a `.env` file in the working directory
4. `default` in the repo `config.toml`
5. `default` in the home `config.toml`
6. `http://homeassistant.local:8123`, with no token

When the server is given by name, the token comes from that server's entry unless `--token` is given. `HASS_TOKEN` is not mixed with a named server: pairing a token from the environment with a server from the config is the mistake this feature is meant to remove. If the named server has no usable token, that is an error.

Inside a Home Assistant add-on, `SUPERVISOR_TOKEN` keeps its present behaviour when no server is given.

### Plugins

Every `*.py` file in `plugins/` is run at the start of a session, home directory first and then the repo's, each in name order. A later file can redefine a name from an earlier one, so a repo can override a helper.

```python
# ~/.config/ha-repl/plugins/20-bookmarks.py
import datetime as dt

sensors = hass.data["entity_components"]["sensor"]


def stale(hours=24):
    """Entities not updated in the last `hours`."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    return [s.entity_id for s in hass.states.async_all() if s.last_updated < cutoff]
```

A file is run as though its contents had been typed at the prompt:

- In live mode the usual [What Runs Where](../../../modes/live_mode.md#what-runs-where) rules apply statement by statement. `stale` above uses `hass`, so it is defined inside Home Assistant; the `import` is local, and is repeated there because `stale` needs it.
- In `api` mode there is no `hass`. Defining `stale` works, since the name is only looked up when it is called, but the `sensors` line fails.
- A statement that fails is reported as a warning on standard error, naming the file and line, and the rest of the file and the remaining files still run. A session always starts.
- Nothing is echoed. The value of a bare expression in a plugin is discarded.

The interactive banner lists the files that were loaded. `--no-plugins` skips them all.

Plugins are also run by `ha-repl exec`, so a snippet and an agent see the same names as the interactive shell. With `--json`, a plugin warning goes to standard error and does not appear in the JSON object.

### Use from other Python contexts

`homeassistant_repl.connect()` reads the same `config.toml`:

```python
obj = await homeassistant_repl.connect("house")
```

Plugins are ordinary Python with no special syntax, so the same file can be named in `PYTHONSTARTUP`, linked into IPython's own startup directory, or run with `exec(open(path).read())` in a notebook. Only the lines that use `hass` depend on live mode.

## Security

A repo's `.ha-repl/` directory is code and configuration that arrives with a clone. Left unchecked it could:

- run arbitrary Python locally, through a plugin
- run arbitrary Python inside whichever Home Assistant is the default, through a plugin that uses `hass`
- run an arbitrary command, through `token_command`
- point `default` at an address of its own choosing

The last of these is the reason the token is never taken from the environment for a server named in a config file.

The proposal is that a repo directory is ignored until it has been approved, as `direnv` does with `.envrc`:

```bash
$ ha-repl live
ha-repl: ignoring /work/mycomponent/.ha-repl - run `ha-repl trust` to allow it
$ ha-repl trust
```

Approval is recorded in `~/.local/state/ha-repl/` against the directory's path and a hash of its contents, so a change to any file in it needs approving again. The home directory is always trusted.

Tokens are never printed, by `ha-repl servers` or in error messages. A `config.toml` that holds a `token` and is readable by other users gets a warning.

## Alternatives Considered

**A separate `--profile` flag.** `--server` would stay as a URL only, and `--profile NAME` would choose an entry. It is more explicit, but gives two flags that both say where to connect and a rule for what happens when both are given.

**More `.env` files.** `.env.house`, `.env.dev` and a flag to pick one. This is close to what exists, but it gives no home directory sharing, no place for startup code, and no way to keep a token out of the file.

**`[tool.ha-repl]` in `pyproject.toml`.** Familiar from `ruff` and `uv`, and no new directory in the repo. It can't hold plugins, so a second location would be needed anyway, and not every component repo has a `pyproject.toml`.

**A single plugin file**, as `PYTHONSTARTUP` has. Simpler, but a directory lets the home and repo sets be combined without one including the other, and lets chezmoi manage each file separately.

**Bookmarks as data**, for example a `[bookmarks]` table of name to expression. Easier to list and to validate, but it can't hold a function or a class, and it would be a format nothing else understands.

**Storing bookmarks inside Home Assistant**, in the server component. They would follow the instance, not the developer, and it adds state to the side of the design that is meant to stay small (see [Design Principles](../design_principles.md)).

## Decisions

The questions this document was circulated with, and how each was settled:

1. **`--server NAME|URL`, or a separate `--profile`?** `--server NAME|URL`.
2. **Should `exec` run plugins by default?** Yes, with `--no-plugins`. The cost is a round trip to Home Assistant for each statement that uses `hass`, on every call.
3. **`.ha-repl/` directory, or a single `.ha-repl.toml`?** The directory, to match the home layout.
4. **Is the trust step worth its cost?** Yes. A repo directory is ignored until `ha-repl trust` has approved it.
5. **Should a server entry be able to refuse writes?** Not in this version.
6. **Should plugins be able to tell which server and mode they are running against?** Yes: `MODE` is `"live"`, `"exec"` or `"api"`, and `SERVER` is the configured server's name, or `None` when connected by URL.

## As Implemented

Points the design above left open, settled in the implementation:

- With `SUPERVISOR_TOKEN` set, the Supervisor is used whenever `--server` is not given, ahead of `HASS_SERVER` and `default`. This is what happened before.
- When the repo file gives a server a token by a different key than the home file did, the repo's replaces it, so a merged server never has two.
- `default` must be the name of a configured server, not a URL.
- What a plugin prints goes to standard error along with its warnings, so `exec` output holds only the snippet's result.
- A warning names the line the failing top-level statement starts on.
- A plugin that can't be parsed is skipped whole, with one warning.
- Outside a git repository, the search for `.ha-repl/` carries on up to the filesystem root.
- Plugins were called startup files in the draft, with a `startup/` directory and a `--no-startup` flag.
- `--auto-await` was added, so that `auto_await = false` in a file can be overridden for one run.

## Out of Scope

- Saving a variable or function from a running session into a plugin
- Plugins that apply to one named server only
- Any change to the server component or its protocol
