"""The names a shell puts in a session before plugins run, declared for
linters and type checkers - nothing is bound here, so import them only
where a type checker looks:

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from homeassistant_repl.plugin import MODE, hass, obj  # noqa: TC004

See docs/configuration/plugins.md. `homeassistant` isn't a dependency of
this package; `hass` is typed wherever the checker can find it, which is any
component's own development environment.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant_api import AsyncClient

    from .sql import SqlTool

__all__ = ["MODE", "SERVER", "hass", "hass_api", "obj", "rebind", "show", "sql"]

MODE: Literal["live", "exec", "api"]
SERVER: str | None  # the configured server's name, None if connected by URL

# live and exec only - a statement using it runs inside Home Assistant
hass: HomeAssistant

# The server's own tree in live and exec, ApiObjTree in api - alike, not identical.
obj: Any

# live and exec only
sql: SqlTool

# Looks inside an object, on whichever side the object is
show: Callable[..., Any]

# Puts back any of these names that has been assigned over, and runs the
# plugins again unless plugins=False
rebind: Callable[..., Any]

# None in api mode if the REST API couldn't be reached
hass_api: AsyncClient | None
