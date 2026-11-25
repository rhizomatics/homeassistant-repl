# Server Installation

This step is only required if you need to use Live or Exec mode - it's not needed for the simpler API mode.

This is a powerful component, so treat with care, especially if its on your home server. It has controls to limit it to Home Assistant API and/or SQL access.

## Marketplace Component


A Home Assistant Marketplace (HACS on older versions of Home Assistant) component that taps into the Home Assistant and acts as a session server over web sockets. It has been designed for HomeAssistant 2026.8 or greater.

Needed for `live` mode only, since `api` mode only uses standard Home Assistant APIs.

Install via Marketplace (in the *Tools* page):

 - it's not in the default Marketplace repository, so you'll have to add `https://github.com/rhizomatics/homeassistant-repl` as a Custom Repository from the top-right dot menu first
 - Search for *Home Assistant REPL* in the Marketplace menu and choose *Download*
 - Restart Home Assistant for it to recognize the new custom component available
 - From **Settings → Devices & services → Add integration** find **Home Assistant REPL Server** in the list and install, there's no further config needed
   - The component will quiz you first to make sure you know what you're doing
   - Alternatively add `ha_repl_server:` to `configuration.yaml`, which is imported as a config entry) 

### If `sql` Can't Be Loaded

`sql` needs two Python packages beyond Home Assistant itself, `nanoarrow` and `sqlparse`. They're installed when the integration starts rather than as its listed requirements, so if they can't be - no build of `nanoarrow` for the platform Home Assistant runs on, say - only `sql` is switched off. `hass`, `obj` and everything else still work, the log says why `sql` isn't there, and so does the shell when it starts.

### Limiting What Is Exposed

**Configure**, on the integration's own page, has a switch for each of the two things a live session can reach: `hass`, and `sql`.

Each switch has a time limit beside it, in minutes. Enter one and that capability is switched off again once the time is up, as if from the form - so it can be opened up for one piece of work without being left on. Leave the limit empty and it stays on.

- The limit is counted from when the form is saved, and carries on through a restart of Home Assistant
- Opening **Configure** again shows the minutes left, to change, or to clear for no limit
- Saving the form, or a limit running out, reloads the integration, which ends any live sessions' server-side state

### Completion's Limits

**Configure** also has a **Limits** section, folded away, setting how much one completion answer can hold, and how long it may take to find it: 500 items and 500 milliseconds unless changed. They keep completing through something very large - every path of a big `obj`, say - from holding up Home Assistant. An answer cut short by either still shows what it found, and narrows as more is typed.

### Stopping Code and the Log

The integration's device has a **Stop running code** button. Pressing it stops whatever is running in every live session - for when the shell that started it has gone, or from a dashboard or automation. Ctrl-C in the shell does the same for its own session.

Every command a shell sends is written to the Home Assistant log, with the user who sent it: the code run and how it went (its value or error, and how long it took), each SQL query and how many rows it returned, and each stop and reset. They're logged at `warning`, so they show in the log with Home Assistant's default settings. They have a logger of their own, so to quieten them without hiding the integration's other warnings, add this to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.ha_repl_server.commands: error
```

### Removing

1. If `ha_repl_server:` is in `configuration.yaml`, take it out first. Otherwise the entry is imported again at the next restart
2. In **Settings → Devices & services → Home Assistant REPL**, open the three-dot menu and choose **Delete**. Any live sessions end, and the Stop button and the integration's device go with it
3. In Marketplace, open *Home Assistant REPL* and choose **Remove**, then restart Home Assistant

`nanoarrow` and `sqlparse`, installed for `sql`, stay in Home Assistant's Python environment until it is next rebuilt (as on an upgrade). Nothing uses them.

## Known Limitations

What the Live Server doesn't do, by design:

- **Code inside Home Assistant runs on its event loop.** Code that blocks it - `time.sleep()`, a blocking HTTP call - holds up all of Home Assistant while it runs, and can't be stopped by Ctrl-C or the Stop button. `await` asynchronous calls, and run blocking ones with `await hass.async_add_executor_job(...)`
- **A session's variables inside Home Assistant last until the integration reloads or Home Assistant restarts.** Saving **Configure**, or a time limit running out, reloads it. Nothing is written to disk
- **One statement at a time per session.** A second command waits for the first to finish; completion still answers meanwhile
- **Only plain data crosses between the shell and Home Assistant** - strings, numbers, and lists and dicts of them. See [What Runs Where](../modes/live_mode.md#what-runs-where)
- **The shell and the Live Server are released together.** Different versions may work with each other for point releases, but aren't tested that way
- **`sql` needs a build of `nanoarrow` for the platform Home Assistant runs on.** Where there isn't one, `sql` is switched off and everything else works
- **It isn't a debugger.** There are no breakpoints or stepping through code

## Troubleshooting

### `... not available: is the ha_repl_server integration installed ...`

- **Cause**: the Live Server isn't installed, or Home Assistant hasn't been restarted since it was downloaded, so its commands don't exist
- **Resolution**: install it as described [above](#marketplace-component), restart Home Assistant, and add the integration. `api` mode works without it

### `... failed: Home Assistant REPL integration is not loaded`

- **Cause**: the integration is installed, but its entry isn't loaded - disabled, failed to start, or reloading
- **Resolution**: open **Settings → Devices & services → Home Assistant REPL**. Enable or reload the entry, and check the log for why it didn't start

### `... failed: Unauthorized`

- **Cause**: every command needs an administrator, and the token belongs to a user who isn't one
- **Resolution**: use a long-lived access token created by an administrator

### `sql` is missing, and a repair issue says it couldn't be loaded

- **Cause**: the packages `sql` needs couldn't be installed or imported - see [If `sql` Can't Be Loaded](#if-sql-cant-be-loaded)
- **Resolution**: if the cause has gone, such as no network while Home Assistant was starting, reload the integration. If `sql` can't run on this platform, switch it off under **Configure**, which also clears the repair issue

### The shell says completion inside Home Assistant needs a newer Live Server

- **Cause**: the shell is newer than the Live Server
- **Resolution**: update the Live Server in Marketplace and restart Home Assistant, so the two are on the same version

### Home Assistant stops responding while code runs

- **Cause**: the code is blocking Home Assistant's event loop - see [Known Limitations](#known-limitations)
- **Resolution**: if it doesn't finish, restart Home Assistant. Run blocking calls with `await hass.async_add_executor_job(...)` next time

### Variables have gone from a session

- **Cause**: the integration was reloaded - **Configure** was saved, or a time limit ran out - or Home Assistant restarted. The shell says which have gone when it reconnects after a restart
- **Resolution**: run the statements that made them again, or put the ones you always want in a [plugin](plugins.md)
