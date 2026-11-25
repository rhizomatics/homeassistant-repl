# RFC0003 - Event Assistance

Improve support for Home Assistant Events for developers.

## Problems to Solve

- What are the events? 
  - Core events
  - Events from built-in integrations
  - Events from Marketplace integrations
- What's the event schema?
- Run this callback from events
- Trigger an event

## Biggest Issue

Home Assistant has no static or dynamic registry of events, they are treated as ephemeral strings with dicts attached, and the only list possible is the one that have active listeners.

Nothing checks an event type when something listens for it. `hass.bus.async_listen` keeps its listeners in a `defaultdict(list)`, so any string is accepted: a typo, or an event from an integration that isn't loaded, gives a listener that never fires. The websocket `subscribe_events` command checks only permission, and an automation's `event` trigger rejects only `state_reported`. This is deliberate, since listening for an event before its source has loaded is legitimate, but it means:

- `hass.bus.async_listeners()` lists what something is listening for, not what fires. A misspelt listener appears in it as if it were real
- An event that fires with nothing listening for it doesn't appear at all

## Direction: An Event Catalogue

Rather than simplifying access to events first, build the catalogue that doesn't exist: incomplete, and not covering Marketplace integrations, but far better than nothing, and the place to send people who are told that no catalogue exists anywhere.

- A scheduled job, weekly to start with, scans Home Assistant core and writes a checked-in JSON file: the constant's name, the event type string, the domain and integration, the module it's declared in, and how it was found
- `ha-repl` uses the file, and it generates web documentation
- A config file holds overrides and corrections, since integrations, even in core, vary in quality and style
- The list is checked against the event types with live listeners, and against what the recorder has seen fire
- Once the catalogue exists, return to showing events in the shell, perhaps in the object tree

Sources, simplest first:

1. `EVENT_*` constants
2. `async_fire` calls, by AST or a language server
3. Event entities and their `event_types`

### Feasibility: First Scan

A rough AST scan of Home Assistant 2026.11.0 (1,564 integrations), module level assignments only:

| Found | Count |
| ----- | ----- |
| `EVENT_*` constants | 318, 36 of them outside `components/` |
| Integrations declaring any | 94 |
| Of those constants, a string literal or `EventType("...")` | 256, 230 distinct values |
| Not events at all: timeouts, schemas, entity descriptions, logbook maps | about 57 |
| Event data keys or event entity types rather than event types (`EVENT_DATA_*`, `EVENT_TYPE_*`) | several dozen |
| Built with an f-string, e.g. `f"{DOMAIN}_recording"` | 5 |
| Values declared by more than one constant | 23 |
| Integrations with an `EventEntity` | 69 |

How `async_fire` is given its event type:

| First argument | Calls |
| -------------- | ----- |
| An `EVENT_*` name | 136 |
| Another name or expression, e.g. `ATTR_HUE_EVENT`, `ZWAVE_JS_NOTIFICATION_EVENT`, a local `event_type` | 44 |
| A string literal | 18, 15 of them matching no constant: `knx_event`, `lutron_event`, `nuki_event`, `xiaomi_aqara.click` … |
| An f-string | 12 |

So:

- Constants are most of it, but not 99%: a step 2 pass on `async_fire` is needed for the literals and for event types held in constants named `*_EVENT` rather than `EVENT_*`
- The `EVENT_` prefix is noisy. A constant used as `async_fire`'s first argument, or as `async_listen`'s, is far stronger evidence than its name alone, so step 2 also cleans up step 1
- f-strings mostly resolve once `DOMAIN` is substituted. The rest, such as an event type held in a variable, go to the overrides file
- `EventEntity` events aren't bus events of their own: they are `state_changed` on the entity, with `event_type` as an attribute. They belong in the catalogue, but as a separate kind

### Phase 1, As Built

- `dev/events/scan.py` parses Home Assistant's source with `ast`, with the standard library only, and resolves imports, so `const.EVENT_X` and `from .const import EVENT_X` count as uses of the constant they name. It writes `dev/events/events.json`
- Each `EVENT_*` constant gets a kind (`event`, `entity_event_type`, `data_key`, `not_event` or `unknown`), whether that's certain, and the evidence for it:
    - **Certain**, from how the code uses it: fired, listened for, typed `EventType`, given as a trigger's `event_type`, or used as an event entity's `event_types` or an event's data key. A value that isn't a string is not an event
    - **Probable**, from heuristics, and only for a value shaped like an event type (no spaces or `/`, with a `_` or `.`): named in a function that fires a variable, as timer's events are, or starting with its integration's domain. A single word, or a value with spaces or a `/`, is probably not an event
- Event types fired as string literals, with no constant, are added as certain events
- `dev/events/overrides.toml` corrects entries by module and constant, and adds events no scan of core can find. An override that matches nothing is reported, so a stale one is noticed
- A weekly GitHub Action scans the latest release and opens a pull request when the catalogue changes - its entries, not just the release or line numbers, which any release moves. Each entry stores its file and line, and one `source_url` template makes the links, so the catalogue isn't rewritten for each release. Merging it publishes the page with the docs
- The page, `/events/`, has CSV and JSON downloads. It was unlisted while under review, and is now in the top menu, the sitemap, site search and `llms.txt`, with `llms-full.txt` given its standard and entity events only

On Home Assistant 2026.11.0: 128 events, 26 event entity types, 4 data keys, 123 not events, and 60 undecided, about half of them Traccar and Netatmo event types carried in another event's data.

Checked against the listeners on a live server, every core event was found, all but `timer.finished` with certainty, except:

- `mobile_app_notification_action` and `ios.action_fired`. The Companion apps name these, and fire them through `mobile_app`'s generic `fire_event` webhook, so they're only in core's source as data. They are in `overrides.toml`, the first of a class: events named outside Home Assistant
- `persistent_notifications_updated`, which nothing in core fires on the bus. It's the name of a dispatcher signal, so the listener is waiting for an event that never comes: the case [Biggest Issue](#biggest-issue) describes, seen in a real installation

## Architecture: Nobody Owns an Event Type

Firing is as unchecked as listening. Beyond a 64 character limit, `async_fire` accepts any event type from any caller, so an integration, or a live mode snippet, can fire `homeassistant_stop` or another integration's events, and every listener for it runs. Firing from outside needs an admin, through the websocket `fire_event` command or the REST API, and is marked `origin=remote`, but almost no listener checks that.

Event types have no namespaces, owners or schemas. The catalogue can supply the first part: which integration declares and fires each event type. With that, "fired by an integration that doesn't own it" is something a tool can point out, whether `ha-repl` when a snippet fires one, or a check across the catalogue itself.

## Open Questions

1. Where the job runs: a GitHub Action in this repo, against the latest Home Assistant release, beta, or both? *Phase 1: this repo, the latest release, weekly*
2. Does the JSON ship inside the `homeassistant-repl` package, be fetched from the docs site, or both?
3. The overrides file's format: per integration, per constant, or per event type? *Phase 1: per module and constant, or event type for one with no constant*
4. Should Marketplace integrations be included later, for example the most installed ones from the Marketplace's own index?
5. Events named outside Home Assistant, such as the Companion apps', can only come from overrides. Is there a source to take them from, such as the Companion app documentation?
6. Should `ha-repl` warn when a snippet fires an event type the catalogue says another integration owns, or a core lifecycle event?