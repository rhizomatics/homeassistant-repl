# Server Installation

This step is only required if you need to use Live or Exec mode - it's not needed for the simpler API mode.

This is a powerful component, so treat with care, especially if its on your home server. It has controls to limit it to Home Assistant API and/or SQL access.

## HACS Component


A HACS component that taps into the Home Assistant and acts as a session server over web sockets. It has been designed for HomeAssistant 2026.8 or greater.

Needed for `live` mode only, since `api` mode only uses standard Home Assistant APIs.

Install via [HACS](https://hacs.xyz):

 - it's not in the default HACS repository, so you'll have to add `https://github.com/rhizomatics/homeassistant-repl` as a Custom Repository from the top-right dot menu first
 - Search for *Home Assistant REPL* in the HACS menu and choose *Download*
 - Restart Home Assistant for it to recognize the new custom component available
 - From **Settings → Devices & services → Add integration** find **Home Assistant REPL Server** in the list and install, there's no further config needed
   - The component will quiz you first to make sure you know what you're doing
   - Alternatively add `ha_repl_server:` to `configuration.yaml`, which is imported as a config entry) 

### Limiting What Is Exposed

**Configure**, on the integration's own page, has a switch for each of the two things a live session can reach: `hass`, and `sql`.

Each switch has a time limit beside it, in minutes. Enter one and that capability is switched off again once the time is up, as if from the form - so it can be opened up for one piece of work without being left on. Leave the limit empty and it stays on.

- The limit is counted from when the form is saved, and carries on through a restart of Home Assistant
- Opening **Configure** again shows the minutes left, to change, or to clear for no limit
- Saving the form, or a limit running out, reloads the integration, which ends any live sessions' server-side state

### Completion's Limits

**Configure** also sets how much one completion answer can hold, and how long it may take to find it: 500 items and 500 milliseconds unless changed. They keep completing through something very large - every path of a big `obj`, say - from holding up Home Assistant. An answer cut short by either still shows what it found, and narrows as more is typed.

### Stopping Code and the Log

The integration's device has a **Stop running code** button. Pressing it stops whatever is running in every live session - for when the shell that started it has gone, or from a dashboard or automation. Ctrl-C in the shell does the same for its own session.

Every command a shell sends is written to the Home Assistant log, with the user who sent it: the code run and how it went (its value or error, and how long it took), each SQL query and how many rows it returned, and each stop and reset. They're logged at `warning`, so they show in the log with Home Assistant's default settings. They have a logger of their own, so to quieten them without hiding the integration's other warnings, add this to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.ha_repl_server.commands: error
```
