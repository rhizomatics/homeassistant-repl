# Secrets with fnox

A long-lived access token has full access to Home Assistant, so it is better kept out of `config.toml`, dotfiles and shell history. [fnox](https://fnox.jdx.dev) keeps secrets encrypted in a TOML file, with [age](https://github.com/FiloSottile/age), the macOS Keychain or a cloud secret store behind it, and hands them to a command when it runs. This page shows how to use it with `ha-repl`, interactively and from a coding agent. [mise](https://mise.jdx.dev) is optional, and covered [at the end](#with-mise).

## Store the Secrets

```bash
fnox init                          # creates fnox.toml and chooses where secrets are kept
fnox -P ha set HA_REPL_TOKEN       # prompts for the token
fnox -P ha set HASS_URL            # e.g. http://homeassistant.local:8123
```

`-P ha` keeps them in a profile of their own, so a command given that profile sees nothing else. Add `-g` to `init` and `set` to use fnox's global config, `~/.config/fnox/config.toml`, instead of a `fnox.toml` in the current directory.

!!! note
    `live` and `exec` modes call the `ha_repl_server` component, which accepts only an administrator's token. `api` mode works with any user's. A token created just for `ha-repl`, by a dedicated user if you like, can be revoked without disturbing anything else.

## Three Ways to Connect

### No Config File

Store the two secrets as `HASS_SERVER` and `HASS_TOKEN` instead, and `fnox exec` gives `ha-repl` everything it needs:

```bash
fnox -P ha exec -- ha-repl exec 'hass.config.as_dict()["version"]'
```

### A Named Server Reading Its Token from the Environment

This keeps the server in `config.toml`, so `ha-repl servers` lists it and it can be the `default`, while the token stays in fnox:

```toml
[servers.home]
url = "http://homeassistant.local:8123"
token_env = "HA_REPL_TOKEN"
```

```bash
fnox -P ha exec -- ha-repl --server home live
```

Run without `fnox exec`, `ha-repl` stops with `environment variable HA_REPL_TOKEN is not set`, and nothing is sent anywhere.

### A Named Server Fetching Its Own Token

`token_command` has `ha-repl` ask fnox itself, so plain `ha-repl --server home` works:

```toml
[servers.home]
url = "http://homeassistant.local:8123"
token_command = "fnox -c /Users/me/.config/fnox/config.toml -P ha get HA_REPL_TOKEN"
```

The command is not run by a shell, so `~` and `$HOME` are not expanded, and it runs in the current directory, so give the config file with `-c` - otherwise a project's own `fnox.toml` can be picked up instead. See [Keeping Tokens in a Vault](client_configuration.md#keeping-tokens-in-a-vault).

This suits a person at a terminal. For agents and scripts, `fnox exec` with `token_env` is the better fit: the token goes only into the environment of the one command, and fnox's `--redact` can hide it in that command's output.

## Agents and Other Unattended Use

If fnox's age key is protected by a passphrase, fnox has to ask for it, and an agent has no terminal to ask on:

```
age: authentication failed: identity file ~/.config/fnox/key.age: could not read passphrase from terminal
```

fnox's daemon solves this. Enable it in the config:

```toml
[daemon]
enabled = true
idle_timeout = "30m"
```

Then unlock once, yourself, in a terminal:

```bash
fnox -c ~/.config/fnox/config.toml -P ha exec -- true
```

The daemon keeps the values in memory and serves them to later commands until it has been idle for `idle_timeout`, or until `fnox daemon stop`. An agent then runs:

```bash
fnox -c /Users/me/.config/fnox/config.toml -P ha --non-interactive exec --redact -- \
  ha-repl exec 'hass.config.as_dict()["version"]'
```

`--non-interactive` makes it fail at once, rather than wait for a passphrase, when nothing is unlocked.

Some things to know, as of fnox 1.48.0:

- A daemon serves one set of profiles and global flags. A read reaches it only with exactly the `-P` set, and the same flags, that unlocked it. Unlock with `-P ha` and read with `-P ha`, not `-P ha,other`.
- With more than one profile in `-P`, `fnox get` prints nothing. Use `exec --only NAME` instead, which works with any set.
- `--redact` hides the exact values of the secrets. `ha-repl` shows the websocket address it connected to (`ws://…/api/websocket`), which is not the same text as `HASS_URL`, so that is not hidden.

## Keeping the URL Out of Dotfiles

`config.toml` needs a literal `url` for each named server. If the address is something you'd rather not publish, for example in a public dotfiles repo, there are two choices.

Use the [no config file](#no-config-file) approach, where the URL comes from fnox every time.

Or have a dotfiles manager write it in. With [chezmoi](https://www.chezmoi.io), a template `dot_config/ha-repl/private_config.toml.tmpl` can read it from fnox when the file is applied:

```toml
default = "home"

[servers.home]
url = {{ output "fnox" "-c" (joinPath .chezmoi.homeDir ".config/fnox/config.toml") "-P" "ha" "get" "HASS_URL" | trim | quote }}
token_env = "HA_REPL_TOKEN"
```

The URL then sits in the file on that machine only, which `private_` makes readable by you alone, and the token is never written. `chezmoi apply` needs fnox unlocked to render it. If applies run unattended, a chezmoi `modify_` script can keep the `url` already in the file when fnox can't be read, instead of failing.

## With mise

[mise](https://mise.jdx.dev) can install both tools and give the fnox command a short name. None of this is needed for the rest of the page.

```toml
# ~/.config/mise/config.toml
[tools]
fnox = "latest"
"pipx:homeassistant-repl" = "latest"    # installed with uv when uv is present

[tasks.ha]
description = "ha-repl with the token from fnox"
run = "fnox -c {{env.HOME}}/.config/fnox/config.toml -P ha --non-interactive exec --redact -- ha-repl"
```

Extra arguments to `mise run` are appended to the task's command:

```bash
mise run ha exec 'hass.config.as_dict()["version"]'
```

Without mise, install `ha-repl` with `uv tool install homeassistant-repl` and use a shell function for the same short name:

```bash
ha() { fnox -c "$HOME/.config/fnox/config.toml" -P ha exec -- ha-repl "$@"; }
```
