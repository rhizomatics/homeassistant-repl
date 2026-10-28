# Roadmap

Project aims are:

- exploring of the APIs in context of a live working instance
- trialling out snippets of code
- debugging code
- hotfixing issues that don't have built in support to do so from existing components.

This is primarily for developers of custom components, and their agents, though may be of interest for other folk tinkering with Home Assistant. It is a potentially sharp tool, so NOT appropriate for general Home Assistant users.

## Back End

A HACS component that taps into the Home Assistant and acts as a session server over web sockets.

## Object Browser

All of the objects in Home Assistant - entities, devices, actions, areas, labels etc - are represented live in an Object Explorer, a tree structure, nested by the "." split name and with the object type and integration_key at the root. The properties of objects can be inspected. 

Logical
```
 - <integration>
   - <domain>
     - <id>
```

Physical
```
 - alexa_devices
   - media_player
     - kitchen_show
   - notify
     - bedroom_echo
   - action
     - play_sound
   - device # maps to a device config entry, same physical device may have several
     - 9949349049030434348434
```

 The object browser makes the largely correct assumption that everything useful lives in an integration

### Enhancements
- Add button reusing Home Assistant dialogs
- Delete, with multi-select, calling regular home-assistant checks
- Copy and paste, similar to how it works in front end for duping automations etc
- Filter by area/label/floor/category/platform/domain/free text
- Handle legacy yaml entities without a `unique_id`
- Actions that don't live in integrations, and other orphans

## REPL

The REPL shell is a full Python REPL shell, (implemented as a VSCode NotebookController?), with multi-line editing, history etc, living inside an asyncio loop that exposes the live Home Assistant instance as

* `hass` - the `HomeAssistant` class at the root of the Python API
* `obj[]` - the object tree exposed as a dictionary object

So I can write code at the command line like:

```python
pir = obj["/rflink/binary_sensor/hall_pir"]
pir.state = "on"  # non-strict mode, sets entity state with repl as context
```

The return value of the object is returned to the shell, value printed and available to Python code as `_`. Tracebacks are printed also, as if they were local (in general everything feels like its local)

There is another special object, `this` which is the object currently highlighted in the Object Browser.

### Strict Mode

This switch controls trade-off between shell convenience and ability to trial working code for a component.

- Control to switch it off in the toolbar of the REPL. 
- `on` 
  - Access to the core API is exactly as it would be in regular component code. 
- `off`
  - Subset of methods that don't require arguments can be accessed like properties, even if they aren't defined that way, and very common ones, like `state` are auto-wired to be gettable and settable.
  - `async` methods are automatically awaited 
  - if method can't be found looks for `async_` instead (could even strip all those off)
  - 'RegistryEntity` and `Entity` merged, `.hass` stripped

### Enhancements
- Drop down button to switch context between different Home Assistant instances
  - Also implies some form of config more structured than env vats
- Read-only Mode
  - Control to switch it off in the toolbar of the REPL. 
  - Caveat about side-effects of supposedly read-only calls and difficulty of knowing what's really read-only. Possibly blanket ban on calling actions.
- Click thru from stacktrace to hass code
- Intellisense, e.g. pop up attribute suggestions when pressing `.`
- Auto clone the source of current HA instance and keep tag aligned with release
  - On demand also for custom components, both this and core can be disabled for space constrained devices, with `inspect.getsource` as backup

## Improvements
- Make the main package more unique and HA specific than `dev_shell` [DONE]
  - `homeassistant-repl`
- Allow it to be set up easily inside any python code (async compatibility)? [DONE]
  - `homeassistant_repl.connect()`
- Efficient transfer of large `sql` results
    - Today a result is built whole in Home Assistant's memory several times over (fetched rows, Arrow arrays, Arrow IPC bytes, base64 text) and sent as one JSON websocket message, about a third larger than the data. `max_rows` is the only protection for a large recorder database
    - Move it to a separate HTTP endpoint in the server component that streams raw Arrow record batches as binary, built chunk by chunk from the database cursor, so server memory stays at one chunk and nothing is base64 encoded
    - Registered as a Home Assistant HTTP view, it keeps what the websocket gave: same port, and Home Assistant's own token authentication
    - Client reads the batches as they arrive and hands them to polars without copying, opening the way to lazy or paged results
- Remove `rich` from the server side [DONE]
    - Tracebacks from inside Home Assistant are rendered there as rich text and shipped as a string; send the raw traceback data and render on the client instead ([debuglater](https://github.com/ploomber/debuglater) may be an option for serializing it)
    - Same for values and `help()` output, so `ha-repl --json exec` gets clean data from both sides

## Other Ideas
- Work inside iPython [DONE]
- VSCode extension
- http caching, using `niquests-cache`
- API Mode
  - API access only, also no need of HACS components [DONE]
  - TBD: whether actions/services get folded into the object tree, or left separate
- Other bindings
  - sql() for the sqlite DB [DONE]
    - or support for a client-side sql client ui
  - api() shortcuts for HA APIs [DONE]
    - https://developers.home-assistant.io/docs/api/rest/
- REPL improvements
  - `rich` formatting [DONE]
  - [iPython features](https://ipython.org/features/)
    - magic commands
    - object introspection -`?` for help, `??` for source code
  - bottom toolbar (prompt-toolkit supports)
- User plugins/extensions [DONE]
  - python scripts in a directory
  - functions
    - add own bindings
    - common shortcuts, e.g. to a sensor or set of entities
- Notebook support, e.g. Marimo [DONE]
- safe(r) mode - remove things like stop
- Event subscription and visualization
- Auto-complete support
  - `jedi`? an LSP?
  - will require to augment rather than replace auto-complete for core python
- Everything runs in the async loop, non async code wrapped as async in HA compatible way. Warn about blocking calls in non-strict
- Persisting context across invocations
- Appdaemon support
- Multiple sessions with a session switcher and optional names
- Ability to reload custom component classes within the shell `reload(x)` for fast feedback debug/development
  - `jurigged`
- Redirect log statements captured 
- Tight authN / authZ / encryption

## Limitations
- NOT a debugger, but may do more to complement one
- NOT pyscript, focuses on actual python even at expense of general usability or home assistance access, no script execution support outside of the repl

## MVP [DONE]

- Basic REPL
  - strict mode only
  - `hass` binding only (entities accessible via usual hass calls)
  - packaged as CLI with `exec` and interactive mode, session name and session reset flag and `HASS_SERVER`
- Object browser
  - out of scope
  - so also no `this` or `obj[]`
- VSCode integration
  - out of scope
- HACS Component
  - server side session management (session reused indefinitely until reset)
  - stdout and traceback reflection to shell
  - admin-only websocket
  - clean up sessions after x hours

## Development

Layout:
- `custom_components/ha_repl_server/`: the HACS integration (named `ha_repl_server` to distinguish it from the CLI below). `session.py` is the execution engine (no HA imports); `websocket_api.py` exposes the admin-only `ha_repl_server/exec`, `ha_repl_server/reset` and `ha_repl_server/sessions` commands.
- `src/homeassistant_repl/`: the `ha-repl` CLI (`exec`, interactive REPL, `reset`, `sessions`).
- `dev/`: a throwaway HA config with `demo:` entities, plus run and bootstrap scripts.
