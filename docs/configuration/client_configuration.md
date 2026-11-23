# Client Configuration

`ha-repl` works with nothing more than `HASS_SERVER` and `HASS_TOKEN`, as described on the [Configuration](index.md) page. A `config.toml` adds names for the Home Assistant servers you work with, so one can be picked by name, and defaults for some of the flags.

It is optional, and everything that worked before still does. The same directory also holds [plugins](plugins.md), Python files run at the start of every session.

## Where It Lives

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

`~/.config` is used on every platform, including macOS. The files are plain text, so they can be kept in a dotfiles manager such as chezmoi.

A repo can carry the same layout in a `.ha-repl/` directory, which is layered over the one in your home directory. It is found by walking up from the working directory, as far as the root of the git repository. It is ignored until you [trust](#trusting-a-repo-directory) it.

## Servers

```toml
default = "dev"          # server used when none is named

[servers.dev]
url = "http://localhost:8123"
token = "eyJ..."             # throwaway dev instance only

[servers.house]
url = "https://ha.example.org"
token_command = "fnox -c /Users/me/.config/fnox/fnox.toml get HA_HOUSE_TOKEN"

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

`token_command` lets the file sit in a public dotfiles repo. A plain `token` suits a chezmoi template that fills it in from a secrets manager; keep such a file private, since `ha-repl` warns when a `config.toml` holding a `token` can be read by other users.

`--server` and `HASS_SERVER` accept either a name or a URL. A value with no `://` in it is looked up as a name.

```bash
ha-repl --server house live
HASS_SERVER=test ha-repl exec 'hass.config.version'
ha-repl live                        # uses `default`
ha-repl --server http://10.0.0.5:8123 --token ... live
```

`ha-repl servers` lists what is configured, which is the default and where each token comes from. It never prints a token.

```bash
$ ha-repl servers
* dev    http://localhost:8123  (token)
  house  https://ha.example.org  (token_command)
  test   http://ha-test.local:8123  (token_env)
```

### Keeping Tokens in a Vault

A long-lived access token has full admin access to Home Assistant, so it's better kept out of `config.toml` altogether. Any secrets tool with a command that prints a secret to standard output works with `token_command`. [fnox](https://fnox.jdx.dev) is a simple one: it keeps secrets encrypted in a TOML file, with [age](https://github.com/FiloSottile/age), the macOS Keychain or a cloud vault behind it.

Store the token once:

```bash
cd ~/.config/fnox
fnox init                    # creates fnox.toml and chooses where secrets are kept
fnox set HA_HOUSE_TOKEN      # prompts for the token
fnox get HA_HOUSE_TOKEN      # check it comes back
```

Then point the server at it:

```toml
[servers.house]
url = "https://ha.example.org"
token_command = "fnox -c /Users/me/.config/fnox/fnox.toml get HA_HOUSE_TOKEN"
```

The command is split like a shell command line but not run by a shell, so:

- `~`, `$HOME` and other variables are not expanded - give paths in full.
- It runs in the current working directory, so pass the config file with `-c` rather than relying on fnox finding a `fnox.toml` from wherever `ha-repl` was started.
- Pipes and redirects don't work. If you need one, wrap it in a script and name that script.
- Anything the command prints to standard error, such as a passphrase or Touch ID prompt, goes straight to the terminal.

The command is run each time `ha-repl` needs the token.

The same pattern works with other tools:

| Vault | `token_command` |
| ----- | --------------- |
| fnox | `fnox -c /Users/me/.config/fnox/fnox.toml get HA_HOUSE_TOKEN` |
| 1Password | `op read op://Private/ha-house/token` |
| macOS Keychain | `security find-generic-password -s ha-house -w` |
| pass | `pass show home-assistant/house` |

#### Using `fnox exec` Instead

fnox can also put secrets into the environment of a command it runs. Pair that with `token_env`, and `ha-repl` reads the token from its environment:

```toml
[servers.house]
url = "https://ha.example.org"
token_env = "HA_HOUSE_TOKEN"
```

```bash
fnox exec -- ha-repl --server house live
```

This suits a project directory with its own `fnox.toml`, where fnox finds the file itself.

### Which Server Is Used

The first of these that gives a value wins:

1. `--server`
2. `HASS_SERVER` in the environment
3. `HASS_SERVER` in a `.env` file in the working directory
4. `default` in the repo `config.toml`
5. `default` in the home `config.toml`
6. `http://homeassistant.local:8123`

A server chosen by name uses its own token, unless `--token` is given. `HASS_TOKEN` is used only with a URL, never with a named server, so a token from the environment can't be sent to an address from a config file.

Inside a Home Assistant add-on that provides a `SUPERVISOR_TOKEN`, `ha-repl` connects through the Supervisor unless `--server` is given.

## Other Settings

Three flags can be given a default at the top of `config.toml`:

```toml
session = "default"      # same as --session
ttl = 30                 # same as --ttl
auto_await = true        # false is the same as --no-auto-await
```

A flag on the command line always wins, and `--auto-await` turns auto-await back on for one run. `HASS_SESSION` comes between the flag and the file.

## Layering a Repo Over Home

The repo `config.toml` is merged over the home one. Top-level keys are replaced, and `servers` is merged by name and then key by key. A typical repo file is one line, choosing the server this component is developed against:

```toml
default = "dev"
```

## Trusting a Repo Directory

A repo's `.ha-repl/` directory arrives with a clone, and it can run Python on your machine and inside Home Assistant, run a command through `token_command`, and choose which server a session connects to. It is therefore ignored until you approve it, as `direnv` does with `.envrc`:

```bash
$ ha-repl live
ha-repl: ignoring /work/mycomponent/.ha-repl - run `ha-repl trust` to allow it
$ ha-repl trust
trusted /work/mycomponent/.ha-repl
  config.toml
  plugins/50-this-component.py
```

Read the files before trusting them. Approval is recorded in `~/.local/state/ha-repl/trusted.json` (under `$XDG_STATE_HOME` if that is set) against the directory's path and a hash of its contents, so a change to any file in it, including one that arrives with a `git pull`, needs approving again. The directory in your home directory is always trusted.

## From Python

`homeassistant_repl.connect()` reads the same `config.toml`, so a script or notebook can use a server by name. See [Alternative Integration](alternative_integration.md).

```python
obj = await homeassistant_repl.connect("house")
```
