# Home Assistant's Debug Console

Home Assistant 2026.11 adds its own Python console, the [Debug console](https://www.home-assistant.io/integrations/debug_console/) integration, described in the [developer blog](https://contributors.home-assistant.io/blog/2026/10/17/debug-console/) a couple of weeks afer this component was first announced.

Once `debug_console:` is added to `configuration.yaml` and Home Assistant restarted, administrators find it under *Settings > Tools > Debug console*: a browser console with `hass` available, top-level `await`, and the last result in `_`.

It covers some of the same ground as `ha-repl`'s [live mode](./modes/live_mode.md), so this page sets out which suits what. The two don't conflict, and can both be enabled on the same instance.

## In Short

Use the **Debug console** to poke around a running instance from a browser - on any machine, a tablet, with nothing to install or configure beyond one line of YAML.

Use **`ha-repl`** to work from a terminal or a coding agent, to work on data with your own local Python libraries, to query the Recorder database, or against a Home Assistant older than 2026.11 - or with nothing installed on Home Assistant at all, in [API mode](./modes/api_mode.md).

## Side by Side

| | Debug console | `ha-repl` |
| --- | --- | --- |
| Where it runs | Browser, in Home Assistant's own UI | Terminal, `ha-repl exec` for scripts and agents, or from plain Python, ipython or Marimo |
| Home Assistant version | 2026.11 or later | Live mode: see [Server Installation](./configuration/server_install.md). API mode works with any version |
| Installed on Home Assistant | Part of core | Live mode: the `ha_repl_server` HACS component. API mode: nothing |
| Enabling | YAML only, then restart | Requires a custom Marketplace install, then added from *Devices & services* after a risk acknowledgement, or YAML. `hass` and `sql` can be switched on separately, with a time limit |
| Where your code runs | All of it inside Home Assistant | Locally, except statements using `hass` or `obj`, which are sent to Home Assistant - see [What Runs Where](./modes/live_mode.md#what-runs-where) |
| Libraries you can import | Only what Home Assistant's Python has installed | Anything installed locally, such as polars or pandas |
| Looking inside objects | Expandable results, with properties only evaluated when clicked | [`show()`](./modes/live_mode.md#looking-inside-objects), printed in colour as text |
| Completion and signatures | Yes, static | Not yet - see [RFC 0002](./developer/design/rfcs/0002-auto-completed.md) |
| Multi-line input and history | Yes, recalled with the arrow keys | Yes, a whole block per history entry, kept across sessions in a history file |
| Stopping running code | *Stop* button or Ctrl-C | Ctrl-C in the shell, a *Stop running code* button in Home Assistant, or `ha-repl exec --timeout` |
| Waiting code | Doesn't block the console | Blocks the prompt until it finishes |
| Database | Through `hass` like anything else | [`sql`](./sql.md), with results downloaded as Arrow for local dataframes or CSV |
| Entities and actions | Through `hass` | Also the [object tree](./obj_tree.md), `obj`, the same in live and API modes |
| Sessions | One per browser tab. Deleted 30 minutes after the tab disconnects, or on restart | Named, and kept until Home Assistant restarts, so `ha-repl exec` calls can share one |
| Audit trail | Each run is logged with the user who ran it | Each command is logged with the user who sent it, its outcome and timing - at `info`, so it needs [a `logger:` setting](./configuration/server_install.md#stopping-code-and-the-log) to show |
| Paste protection | Pasting blocked until you type `allow pasting` | None - the terminal's own paste is used |
| Extending | No | [Plugins](./configuration/plugins.md), which a component can ship for its developers |

## Things in Common

- Both are for developers, not everyday use, and are admin only.
- Code inside Home Assistant runs on its event loop in both. A blocking call such as `time.sleep()` freezes Home Assistant, and nothing can interrupt it. `await` asynchronous calls, and run blocking work with `await hass.async_add_executor_job(...)`.
- Both give full access to everything Home Assistant holds, including passwords and tokens. Never run code you don't understand.

## Security Choices

The Debug console can only be enabled in YAML: the Home Assistant team chose that so code in an administrator's browser, such as a custom card, can't switch it on. `ha-repl`'s server component can be added from the UI as well as YAML, as most integrations can. If that matters for your instance, add it with `ha_repl_server:` in YAML, and use the [time limits](./configuration/server_install.md#limiting-what-is-exposed) on `hass` and `sql` so they don't stay on.
