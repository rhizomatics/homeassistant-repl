"""Constants for Home Assistant REPL."""

DOMAIN = "ha_repl_server"
DEFAULT_SESSION = "default"

# What the integration's options can switch on and off: `expose_<name>`,
# with `expose_<name>_until` (an ISO time) when it is on only for a while.
CAPABILITIES = ("hass", "sql")

# What `sql` needs, beyond Home Assistant itself. Not manifest requirements:
# installed and imported at setup instead (see __init__.py's _load_sql), so
# if they can't be - no wheel for this platform, say - only `sql` is lost,
# not the whole integration.
SQL_REQUIREMENTS = ["nanoarrow>=0.9.0", "sqlparse>=0.5.0"]

# Rows a query gives back unless it says otherwise.
DEFAULT_MAX_ROWS = 1000

# Completion's limits, as the integration's options give them.
DEFAULT_COMPLETE_LIMIT = 500
DEFAULT_COMPLETE_BUDGET_MS = 500
