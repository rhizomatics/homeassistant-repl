"""API client mode's interactive shell: same line editing/history as
repl.py, but code runs right here (LocalSession) - no `hass`, no
ha_repl_server/exec call, no HACS component required on the HA side at
all. `obj` is bound the same name as live mode, just to an ApiObjTree
instead of the live ObjTree, so a snippet that only touches `obj` runs
unchanged in either mode. `hass_api` is bound the same way - an
already-connected homeassistant_api.AsyncClient, not a function to call.

The cache refresh happens here, between prompts, not inside ApiObjTree's own
Mapping methods - see api_objtree.py's module docstring for why (asyncio
reentrancy: this loop is the one safe place that's both async and knows when
"between commands" is).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from .api_objtree import ApiObjTree, Cache
from .client import Client, HaReplError, client_version
from .completer import HaCompleter
from .editor import python_prompt, run_interruptibly
from .local_session import LocalSession
from .plugins import bind_context, bind_rebind, local_runner, run_plugins
from .repl import banner_address, banner_plugins, is_quit_call
from .rest import hass_api
from .shellhelp import answer, help_request

HISTORY = Path.home() / ".ha_repl_api_history"


async def run_api_repl(
    client: Client,
    ttl: float,
    *,
    auto_await: bool = True,
    server_name: str | None = None,
    plugins: Sequence[Path] = (),
) -> int:
    cache = Cache(client, ttl)
    await cache.refresh()
    try:
        rest_client = await hass_api(client.url, client.token)
    except HaReplError as err:
        print(f"ha-repl: hass_api unavailable: {err}", file=sys.stderr)
        rest_client = None

    session = LocalSession(
        {"obj": ApiObjTree(cache), "hass_api": rest_client}, auto_await=auto_await
    )

    completer = HaCompleter(session.globals_)
    prompt = python_prompt(HISTORY, completer)
    bind_context(session.globals_, "api", server_name)
    bind_rebind(session.globals_, plugins, local_runner(session))
    loaded = await run_plugins(plugins, local_runner(session))
    print(
        f"API connected to {banner_address(client.url, server_name)}. "
        f"Client v{client_version()}. "
        "`obj` (entities and actions) and `hass_api` (REST client) - no `hass`. Ctrl-D to exit."
        f"{banner_plugins(loaded)}"
    )
    while True:
        try:
            source = await prompt.prompt_async(">>> ")
        except KeyboardInterrupt:
            continue
        except EOFError:
            return 0
        if not source.strip():
            continue
        if (asked := help_request(source)) is not None:
            await answer(
                asked, "api", session.run, completer.locate, completer.describe
            )
            continue
        if is_quit_call(source):
            return 0
        if cache.is_stale():
            await cache.refresh()
        await run_interruptibly(session.run(source))
