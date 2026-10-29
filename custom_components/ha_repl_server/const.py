"""Constants for Home Assistant REPL."""

DOMAIN = "ha_repl_server"
DEFAULT_SESSION = "default"

# What the integration's options can switch on and off: `expose_<name>`,
# with `expose_<name>_until` (an ISO time) when it is on only for a while.
CAPABILITIES = ("hass", "sql")

# Completion's limits, as the integration's options give them.
DEFAULT_COMPLETE_LIMIT = 500
DEFAULT_COMPLETE_BUDGET_MS = 500
