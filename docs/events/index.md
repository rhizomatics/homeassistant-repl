---
title: Event Catalogue
description: The event types Home Assistant core declares and fires, found by scanning its source.
unlisted: true
search:
  exclude: true
---
# Event Catalogue

Home Assistant has no registry of the events it fires: an event type is any string given to `hass.bus.async_fire`. This catalogue is built by scanning Home Assistant core's source each week, without running any of it. It starts from the `EVENT_*` constants integrations declare, then adds the event types fired as plain strings. What each constant is depends on how the code uses it, not on its name, since the `EVENT_` prefix is also used for event data keys, event entity types and settings. Corrections and events the scan can't find come from [`dev/events/overrides.toml`](https://github.com/rhizomatics/homeassistant-repl/blob/main/dev/events/overrides.toml).

!!! warning "A first draft"
    Not yet reviewed. Some verdicts are heuristics, marked *probably*, and Marketplace integrations aren't covered.

<!-- event-catalogue -->
