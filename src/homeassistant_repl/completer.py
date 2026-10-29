"""Tab completion in the shells - see RFC 0002.

Local code is completed by complete.py's walk, which runs nothing, and by
jedi where the walk can't follow the expression (a loop variable, an
import). In live mode, a statement that reads anything living in Home
Assistant is completed there instead, by ha_repl_server/complete running
the same walk against the server session - the rule LiveSession.run() uses
to decide where it runs, so the two can't disagree. A bare name completes
from both sides at once.

Completion pops up while typing after a `.`, inside a key or path string,
and after two letters of a name; Tab always asks. A remote answer slower
than REMOTE_WAIT shows nothing rather than holding up the prompt, and is
kept for the next keystroke - answers are kept until the next command runs,
and narrowed locally as more is typed.
"""

from __future__ import annotations

import asyncio
import io
import keyword
import tokenize
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

import jedi  # type: ignore[import-untyped]
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from .client import HaReplError
from .complete import Item, complete, context_start, kind_at

if TYPE_CHECKING:
    from .repl import LiveSession

# The same rule as complete.py's, for jedi: nothing is evaluated (though
# jedi still calls __getattr__ and __dir__ - see RFC 0002's Step 0 Findings).
jedi.settings.allow_unsafe_interpreter_executions = False

# Seconds to wait for Home Assistant while typing, and after Tab.
REMOTE_WAIT = 0.3
TAB_WAIT = 2.0


def names_read(text: str) -> set[str]:
    """The names in `text`, which may well not be finished Python."""
    names: set[str] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NAME and not keyword.iskeyword(token.string):
                names.add(token.string)
    except tokenize.TokenError, SyntaxError:
        pass  # what's typed so far still counts
    return names


class HaCompleter(Completer):
    def __init__(
        self, namespace: dict[str, Any], live: LiveSession | None = None
    ) -> None:
        self.namespace = namespace
        self.live = live
        # Whether the server can complete: False for one older than this.
        self.remote = live is not None
        self._answers: dict[str, dict[str, Any]] = {}
        self._asking: dict[str, asyncio.Future[dict[str, Any] | None]] = {}

    def forget(self) -> None:
        """Drop what Home Assistant said - after a command, which may have
        changed what's there."""
        self._answers.clear()

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        """Local only, and waiting for jedi: for a caller that can't wait
        asynchronously. The shells use get_completions_async."""
        text = document.text_before_cursor
        if (found := self._walk(document)) is not None:
            yield from found
        else:
            yield from self._jedi(document)
        if kind_at(text) == "name":
            yield from self._remote_names(text)

    async def get_completions_async(
        self, document: Document, complete_event: CompleteEvent
    ) -> Any:
        text = document.text_before_cursor
        if not _worth_asking(text, complete_event):
            return
        if self.live is not None and self.remote and self.live.completes_remotely(text):
            wait = TAB_WAIT if complete_event.completion_requested else REMOTE_WAIT
            answer = await self._ask_remote(text, wait)
            if answer is not None:
                for completion in answer:
                    yield completion
                return
            # Not something Home Assistant could follow: try here instead.
        if (found := self._walk(document)) is None:
            found = await asyncio.to_thread(self._jedi, document)
        for completion in found:
            yield completion
        if kind_at(text) == "name":
            for completion in self._remote_names(text):
                yield completion

    def _walk(self, document: Document) -> list[Completion] | None:
        found = complete(document.text, document.cursor_position, self.namespace)
        if found is None:
            return None
        return [_completion(item, found.replace) for item in found.items]

    def _jedi(self, document: Document) -> list[Completion]:
        try:
            script = jedi.Interpreter(document.text, [self.namespace])
            found = script.complete(
                document.cursor_position_row + 1, document.cursor_position_col
            )
        except Exception:  # noqa: BLE001 - jedi failing is nothing to complete
            return []
        typed = _typed(document.text_before_cursor)
        return [
            Completion(
                c.name_with_symbols,
                -c.get_completion_prefix_length(),
                display_meta=c.type,
            )
            for c in found
            if typed.startswith("_") or not c.name.startswith("_")
        ]

    def _remote_names(self, text: str) -> Iterator[Completion]:
        """A bare name can be one living in Home Assistant too."""
        if self.live is None:
            return
        typed = _typed(text)
        for name in sorted(self.live.remote_names_now() - set(self.namespace)):
            if name.startswith(typed) and (typed.startswith("_") or name[:1] != "_"):
                yield Completion(name, -len(typed), display_meta="HA")

    async def _ask_remote(self, text: str, wait: float) -> list[Completion] | None:
        """Home Assistant's completions, None if it couldn't follow the
        expression (or didn't answer in time, or can't complete at all)."""
        typed = _typed(text)
        # Ask about the context alone, and narrow by what's typed here.
        source = text[: len(text) - len(typed)] + ("_" if typed.startswith("_") else "")
        answer = self._answers.get(source)
        if answer is None:
            asking = self._asking.get(source)
            if asking is None:
                asking = self._asking[source] = asyncio.ensure_future(
                    self._fetch(source)
                )
                asking.add_done_callback(lambda _: self._asking.pop(source, None))
            done, _ = await asyncio.wait({asking}, timeout=wait)
            if not done:
                return []
            answer = asking.result()
            if answer is None:
                return None
        if answer["incomplete"] and typed:
            # More than one answer holds: ask about exactly what's typed.
            answer = await self._fetch(text, keep=False)
            if answer is None:
                return None
            typed = ""
        if not answer["resolved"]:
            return None
        return [
            _completion(Item(**item), len(_typed(text)), remote=True)
            for item in answer["items"]
            if item["label"].startswith(typed)
        ]

    async def _fetch(self, source: str, *, keep: bool = True) -> dict[str, Any] | None:
        assert self.live is not None
        try:
            answer = await self.live.client.call(
                "ha_repl_server/complete",
                session=self.live.session_name,
                source=source,
                cursor=len(source),
            )
        except HaReplError as err:
            if err.code == "unknown_command":
                self.remote = False
            return None
        if keep:
            self._answers[source] = answer
        return answer


def _typed(text: str) -> str:
    """The part of the word or key being completed that's already typed."""
    return text[context_start(text) :]


def _worth_asking(text: str, event: CompleteEvent) -> bool:
    """Tab always; while typing, only once there's something to go on."""
    if event.completion_requested:
        return True
    kind = kind_at(text)
    if kind != "name":
        return True
    typed = _typed(text)
    return len(typed) >= 2 and not typed[0].isdigit()


def _completion(item: Item, replace: int, *, remote: bool = False) -> Completion:
    meta = item.detail or item.kind
    return Completion(
        item.label, -replace, display_meta=f"HA {meta}" if remote else meta
    )
