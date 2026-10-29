# Home Assistant REPL

A REPL shell custom designed for Home Assistant custom component developers, tinkerers and native Python speakers. Makes it easy as pie!

It is an opinionated REPL ([Read-Eval-Print-Loop](https://en.wikipedia.org/wiki/Read%E2%80%93eval%E2%80%93print_loop)) shell that aims are to make it easier without any configuration to:

- Exploring of the APIs in context of a live working instance
- Trialling out snippets of code
- Debugging code (but see note below)
- Hotfixing issues that don't have built in support to do so from existing components.

uvx --from homeassistant-repl ha-repl --token=<insert token here>Home Assistant REPL (API client mode) connected to ws://homeassistant.local:8123/api/websocket. `obj` only, read-only - no `hass`. Ctrl-D to exit.obj["/mqtt/binary_sensor/kitchen_terrace_window_tilt"].state'off'\[o.name for o in obj["/rflink/binary_sensor"].values() if o.state=='unavailable'\]['Shed Intruder Alarm', 'Panic Keyfob']{(o.entity_id, o.state) for o in obj.find(domain="binary_sensor",platform="mqtt")}{\
('binary_sensor.terrace_pir_occupancy', 'unavailable'),\
('binary_sensor.scullery_smoke_alarm_battery_low', 'off'),\
('binary_sensor.kitchen_terrace_window_tweaked', 'off'),\
('binary_sensor.scullery_water_detector_water_leak', 'dry'),\
('binary_sensor.boiler_co_detector_battery_low', 'off'),\
('binary_sensor.pantry_sensor_occupancy', 'off')\
}

> > >

If you're not already comfortable using Python tools to manipulate data on the fly, or better REPL shells in other languages, this is a great way to learn, and faster at the keyboard than clicking around Jupyter notebooks.

This is primarily for developers of custom components, and their LLM agents, though may be of interest for other folk tinkering with Home Assistant. It is a potentially sharp tool, so NOT appropriate for general Home Assistant users.

## Features

- Integrated with `rich` and `pygments` for pretty object printouts and stack traces
- Reuses session and history features from `prompt-toolkit`
- Access to all entities via an [Object Tree](https://homeassistant-repl.rhizomatics.org.uk/obj_tree/index.md) dictionary like interface
- Multi-line editing, with a whole statement or block per history entry - Up brings back the complete `for` loop, to edit any line of it
- Integrated with [homeassistant-api](https://pypi.org/project/HomeAssistant-API/) as `hass_api` object
- Auto-awaits coroutines for easy shell use (can be switched off or overridden)
- Dedicated shell that can be run without installation with `uv`
- Add in your own [plugins](https://homeassistant-repl.rhizomatics.org.uk/configuration/plugins/index.md) or package up a plugin with your component to help other developers
- Usable from inside `ipython` shell, Marimo notebooks or plain `python -m asyncio`

All of the above works with standard Home Assistant APIs, referred to as [`api` mode](https://homeassistant-repl.rhizomatics.org.uk/modes/api_mode/index.md).

Home Assistant REPL also has an advanced [`live` mode](https://homeassistant-repl.rhizomatics.org.uk/modes/live_mode/index.md) that taps directly into a live Home Assistant using an optional server component available via [HACS](http://hacs.xyz).

Get started with a single line at [Quick Start](https://homeassistant-repl.rhizomatics.org.uk/quick_start/index.md)

## Advanced Modes

These modes require a custom component to be [installed](https://homeassistant-repl.rhizomatics.org.uk/configuration/server_install/index.md) on the target Home Assistant server via HACS, or use the supplied scripts to install on a local devcontainer. It adds:

- Full access to the core Home Assistant Python API via `hass`
- Read/write access to the actual objects, e.g. entities and their helpers
- Agent friendly non-interactive mode using [Exec Mode](https://homeassistant-repl.rhizomatics.org.uk/modes/exec_mode/index.md)
- A frisson of danger

## Future Developments

See the [Roadmap](https://homeassistant-repl.rhizomatics.org.uk/developer/design/roadmap/index.md) for where this might go, and your feedback welcome.

Note

It is not intended to ever be a replacement for a Python debugger, although it may complement one. It also does not intend to replicate [PyScript](https://pyscript.net), instead focusing on standard python (PyScript uses MicroPython) even at expense of general usability or home assistance access, and not a general automation script execution service. For most non-developer cases, [homeassistant-cli](https://pypi.org/project/homeassistant-cli/) is a better choice, with pre-packaged access to devices, entities, services etc.

## Home Assistant's Debug Console

Home Assistant 2026.11 added its own browser-based Python console, the [Debug console](https://www.home-assistant.io/integrations/debug_console/). See [Debug Console](https://homeassistant-repl.rhizomatics.org.uk/debug_console/index.md) for how it compares, and which suits what.

## Agent Support

For coding agents, there is [Exec Mode](https://homeassistant-repl.rhizomatics.org.uk/modes/exec_mode/index.md) and a [marketplace skill](https://homeassistant-repl.rhizomatics.org.uk/modes/exec_mode/#agent-skill).

## Other Python REPLs

These are all current with Python v3, there are others like dreampie that didn't make it past Python 2.

### Out of the Box

- [Home Assistant Debug console](https://www.home-assistant.io/integrations/debug_console/) - browser console inside Home Assistant itself, from 2026.11 - see [comparison](https://homeassistant-repl.rhizomatics.org.uk/debug_console/index.md)
- [Python Interactive Mode](https://docs.python.org/3/tutorial/appendix.html#tut-interac) - Core python REPL, greatly enhanced in v3.13 with PyPi code, and now the default Python terminal experience.
- [VSCode Python REPL](https://code.visualstudio.com/docs/python/run#_native-repl) - Notebook style with Intellisense, bundled with the standard Microsoft VSCode Python bundle

### Classics

- [Terminal iPython](https://ipython.org) - Progenitor of Jupyter notebooks, rich formatting, syntax help, shell integration etc
- [ptpython](https://github.com/prompt-toolkit/ptpython) - Syntax help and formatting, mouse support and more. Part of the [prompt-toolkit](https://python-prompt-toolkit.readthedocs.io/en/latest/) project, also used by Home Assistant REPL.
- [IDLE](https://docs.python.org/3/library/idle.html) - the original Python Foundation enhanced shell, colourized text, multi-window etc

### Others

- [nrepl-python](https://git.sr.ht/~ngraves/nrepl-python) - based on the Clojure `nrepl` protocol, a simpler alternative to LSP
- [bpython](https://bpython-interpreter.org) - Syntax help, auto-complete, improved history
- [xon.sh](https://xon.sh) - python-centric shell
