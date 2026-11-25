"""Every test in test_complete.py, run again against the Live Server's copy of
complete.py rather than the shell's.

The two copies are the same file (test_shell_and_server_copies_are_the_same),
but coverage is counted per file, and the quality scale's test-coverage rule
asks for it in every module of the integration. So rather than a second set
of tests to keep in step, test_complete.py's own source is run here with its
imports pointing at the server's copy: a test added there counts for both.
"""

from __future__ import annotations

from pathlib import Path

_SOURCE = Path(__file__).with_name("test_complete.py")

# About finding the shell's own source on GitHub, by its path in this repo
_SHELL_ONLY = ("test_source_links_to_the_version_running",)

_code = (
    _SOURCE
    .read_text(encoding="utf-8")
    .replace(
        "from homeassistant_repl import complete as complete_module",
        "from custom_components.ha_repl_server import complete as complete_module",
    )
    .replace("homeassistant_repl.complete", "custom_components.ha_repl_server.complete")
)
assert "custom_components.ha_repl_server" in _code, "test_complete.py's imports changed"
exec(compile(_code, str(_SOURCE), "exec"), globals())

for _name in _SHELL_ONLY:
    del globals()[_name]
