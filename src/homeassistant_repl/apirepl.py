"""API client mode's interactive shell: same local line editing/history as
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

import ast
import codeop
import sys
from collections.abc import Sequence
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import PygmentsLexer
from pygments.lexers.python import PythonLexer

from .api_objtree import ApiObjTree, Cache
from .client import Client, HaReplError, client_version
from .local_session import LocalSession
from .plugins import bind_context, bind_rebind, local_runner, run_plugins
from .repl import banner_address, banner_plugins, is_quit_call
from .rest import hass_api

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

    compiler = codeop.CommandCompiler()
    compiler.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    prompt: PromptSession[str] = PromptSession(
        history=FileHistory(str(HISTORY)), lexer=PygmentsLexer(PythonLexer)
    )
    bind_context(session.globals_, "api", server_name)
    bind_rebind(session.globals_, plugins, local_runner(session))
    loaded = await run_plugins(plugins, local_runner(session))
    print(
        f"API connected to {banner_address(client.url, server_name)}. "
        f"Client v{client_version()}. "
        "`obj` (entities and actions) and `hass_api` (REST client) - no `hass`. Ctrl-D to exit."
        f"{banner_plugins(loaded)}"
    )
    lines: list[str] = []
    while True:
        try:
            line = await prompt.prompt_async("... " if lines else ">>> ")
        except KeyboardInterrupt:
            lines.clear()
            continue
        except EOFError:
            return 0
        lines.append(line)
        source = "\n".join(lines)
        try:
            # None means incomplete input: keep reading continuation lines.
            if compiler(source, "<ha_repl>", "single") is None:
                continue
        except SyntaxError, OverflowError, ValueError:
            pass  # run it anyway so LocalSession reports the error consistently
        lines.clear()
        if not source.strip():
            continue
        if is_quit_call(source):
            return 0
        if cache.is_stale():
            await cache.refresh()
        await session.run(source)
