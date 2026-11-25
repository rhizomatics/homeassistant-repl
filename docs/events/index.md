---
title: Event Catalogue
description: The event types Home Assistant core declares and fires, found by scanning its source.
---
# Event Catalogue

An inventory of known Home Assistant [Events](https://www.home-assistant.io/docs/configuration/events/), fired on its internal [Event Bus](https://contributors.home-assistant.io/core/internals/events/).

## How and Why

Home Assistant has *no registry of the events it fires*: an event type is any string given to `hass.bus.async_fire`. 

This catalogue is built by regularly scanning Home Assistant core's source, without running any of it to capture live behaviour. It starts from the `EVENT_*` constants that most integrations declare, then adds the event types fired as plain strings. Each of these constants is described based on how the code uses it, not on its name, since the `EVENT_` prefix is also used for event data keys, event entity types and settings. 

Corrections and events the scan can't find are registered here in [`overrides.toml`](https://github.com/rhizomatics/homeassistant-repl/blob/main/dev/events/overrides.toml).

## Limitations

This registry is not complete, some events are non-events, and some events may be missing. It does however have most of them, and feedback welcomed on either methodological improvements or one-off corrections.

!!! note "Home Assistant core only"
    Only Home Assistant itself and the integrations that ship with it are covered. Events fired by Marketplace integrations, or any other custom component, aren't in the catalogue, though they share the same event bus.

<!-- event-catalogue -->
