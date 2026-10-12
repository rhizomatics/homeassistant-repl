# Quick Start

## No Server Install

Use the `api` mode with `uv` (traditional install also available via `pip install homeassistant-repl`).

If Home Assistant is available via `http://homeassistant.local:8123` then you can skip the `--server` argument. 

If not running inside a Home Assistant add-on, like [Studio Code Server](https://github.com/hassio-addons/app-vscode) or [Advanced SSH & Web Terminal](https://github.com/hassio-addons/app-ssh),you will also need to create a [Long Lived Access Token](https://developers.home-assistant.io/docs/auth_api/#long-lived-access-token) by going to the personal settings on your Home Assistant mobile or desktop app.

```bash
uvx --from homeassistant-repl ha-repl --token <<<my long lived access token>>>
```

Or install the command so all you have to type is `ha-repl` in future:

```bash
uv tool install homeassistant-repl
ha-repl
```

## First Steps with the Shell

The shell is a full Python REPL shell, with multi-line editing, history etc, living inside an asyncio loop that exposes the live Home Assistant instance as:

* `obj` - the [Object Tree](./obj_tree.md) exposed as a dictionary object and common methods
* `hass_api` - the Home Assistant web socket / REST API exposed using the [homeassistant-api](https://pypi.org/project/HomeAssistant-API/) library

In `live` mode it offers, in addition to what `api` mode offers:

* `hass` - the `HomeAssistant` class at the root of the Python API
* `sql`  - [SQL access](./sql.md) to the Recorder databases
* `show(thing)` - [looks inside](./modes/live_mode.md#looking-inside-objects) any `hass` object, with its attributes and their values in colour

So I can write code at the command line like:

```python
>>> pir = obj["/rflink/binary_sensor/hall_pir"]
>>> pir.state = "on"  # non-strict mode, sets entity state with repl as context
```

The return value of the object is returned to the shell, value printed and available to Python code as `_`. Tracebacks are printed also, as if they were local (in general everything feels like its local)

The `homeassistant-api` integration can be used like:

```python
>>> len(hass_api.get_domain('cover'))
12
```

!!! note
    The `hass_api` object returns `AsyncEntity` objects rather than `ApiEntity` objects - this is because it comes from a separate project, [homeassistant-api](https://pypi.org/project/HomeAssistant-API/) that is focused solely on API development, whereas the `ApiEntity` class is designed for custom code development, being a subset of the real `Entity` in HomeAssistant, that you'd also get in `live` mode.

See also [Alternative Integration](configuration/alternative_integration.md) options for how to use this in your own plain python shell, ipython, Marimo or similar.



## Starting the Shell

Use the `HASS_SERVER` environment variable, exported or in a local `.env` file, or the `--server` command line argument if the Home Assistant server is not running locally at usual address( i.e. `http://homeassistant.local:8123`). A [long lived access token](https://developers.home-assistant.io/docs/auth_api/#long-lived-access-token) is needed at `--token` or in an `HASS_TOKEN` environment variable.

The quickest way to run the shell is using *uv*, which you can do without cloning this repo or making any other downloads.

```bash
uv run --with homeassistant-repl ha-repl         
```
Get help on the arguments in the usual way,

```bash
uv run --with homeassistant-repl ha-repl --help       ,
```