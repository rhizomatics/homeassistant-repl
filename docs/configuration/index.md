title: Configuration
---
# Configuration

## Tokens

All modes require a [Long Lived Access Token](https://developers.home-assistant.io/docs/auth_api/#long-lived-access-token) which can be provided via `HASS_TOKEN` enivironment variable, a `HASS_TOKEN=xxxx` line in an `.env` file, or the `--token` argument on `ha-repl`.

## Server

The URL will default to `http://homeassistant.local:8123` or can be set using the `HASS_SERVER` enivironment variable, a `HASS_SERVER=xxxx` line in an `.env` file, or the `--server` argument on `ha-repl`.

!!! note
    Although `homeassistant-api` itself has different env vars, when used within `ha-repl` it will be set up automatically using the same server and token as the main shell.

## Configuration File

A configuration file makes it easy to work with more than one Home Assistant, for example a `beta` and `latest` Docker instance, or your real home server. 

Name them in `~/.config/ha-repl/config.toml` and pick one with `--server NAME`. 

This configuration setup also supports plugins, which are simply Python files in `~/.config/ha-repl/plugins/` and are run at the start of every session, for helper functions and bookmarked expressions. 

See [Client Configuration](client_configuration.md) for more info.

{{ pagetree(siblings) }}
