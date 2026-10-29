"""Tab completion in the shells - see RFC 0002.

Local code is completed by complete.py's walk, which runs nothing, and by
jedi where the walk can't follow the expression (a loop variable, an
import). In live mode, a statement that reads anything living in Home
Assistant is completed there instead, by ha_repl_server/complete running
the same walk against the server session - the rule LiveSession.run() uses
to decide where it runs, so the two can't disagree. A bare name completes
from both sides at once.

Inside a call's brackets, SignatureHelp shows its signature under the
prompt, decided the same way.

Completion pops up while typing after a `.`, inside a key or path string,
and after two letters of a name; Tab always asks. A remote answer slower
than REMOTE_WAIT shows nothing rather than holding up the prompt, and is
kept for the next keystroke - answers are kept until the next command runs,
and narrowed locally as more is typed.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import keyword
import tokenize
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

import jedi  # type: ignore[import-untyped]
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples

from .client import HaReplError, client_version
from .complete import (
    Doc,
    Item,
    Signature,
    Source,
    call_at,
    complete,
    context_start,
    describe,
    kind_at,
    matches,
    rank,
    signature,
    source,
)

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

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
        self.signatures = SignatureHelp(self)

    def forget(self) -> None:
        """Drop what Home Assistant said - after a command, which may have
        changed what's there."""
        self._answers.clear()
        self.signatures.forget()

    async def locate(self, expression: str) -> Source | None:
        """Where the source of what `expression` names is, for `thing??` -
        asked of Home Assistant if that's where it lives."""
        if self.asks_remotely(expression + "."):
            found = await self._ask_about("source", expression)
            return Source(**found) if found else None
        return source(
            expression, self.namespace, {"homeassistant_repl": client_version()}
        )

    async def describe(self, expression: str) -> Doc | None:
        """What `expression` names, for Ctrl-Space and `ha-repl inspect` -
        asked of Home Assistant if that's where it lives."""
        if self.asks_remotely(expression + "."):
            found = await self._ask_about("doc", expression)
            return Doc(**found) if found else None
        return describe(expression, self.namespace)

    async def _ask_about(self, kind: str, expression: str) -> dict[str, Any] | None:
        assert self.live is not None
        try:
            answer = await self.live.client.call(
                "ha_repl_server/complete",
                kind=kind,
                session=self.live.session_name,
                source=expression,
                cursor=len(expression),
            )
        except Exception:  # noqa: BLE001 - not answered, for whatever reason
            return None
        return answer.get(kind)

    def asks_remotely(self, text: str) -> bool:
        return (
            self.live is not None and self.remote and self.live.completes_remotely(text)
        )

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
        if self.asks_remotely(text):
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
        # Asked with the word being typed left out, since jedi only matches
        # the start of a name, and narrowed here by matches().
        typed = _typed(document.text_before_cursor)
        start = document.cursor_position - len(typed)
        bare = Document(
            document.text[:start] + document.text[document.cursor_position :], start
        )
        try:
            script = jedi.Interpreter(bare.text, [self.namespace])
            found = script.complete(
                bare.cursor_position_row + 1, bare.cursor_position_col
            )
        except Exception:  # noqa: BLE001 - jedi failing is nothing to complete
            return []
        found = sorted(
            (c for c in found if matches(c.name, typed)),
            key=lambda c: rank(c.name, typed),
        )
        return [
            Completion(c.name_with_symbols, -len(typed), display_meta=c.type)
            for c in found
        ]

    def _remote_names(self, text: str) -> Iterator[Completion]:
        """A bare name can be one living in Home Assistant too."""
        if self.live is None:
            return
        typed = _typed(text)
        for name in sorted(self.live.remote_names_now() - set(self.namespace)):
            if matches(name, typed):
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
        items = [
            Item(**item) for item in answer["items"] if matches(item["label"], typed)
        ]
        if typed:
            items.sort(key=lambda i: (i.kind != "parameter", *rank(i.label, typed)))
        return [_completion(item, len(_typed(text)), remote=True) for item in items]

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
        except Exception:  # noqa: BLE001 - a lost connection is no completions
            return None
        if keep:
            self._answers[source] = answer
        return answer


class SignatureHelp:
    """The signature of the call being typed, under the prompt, with the
    argument the cursor is on in bold - shown only inside a call's brackets.
    Found locally, or by Home Assistant by the same rule as completion, and
    kept, by the call, until the next command runs."""

    def __init__(self, completer: HaCompleter) -> None:
        self.completer = completer
        self._session: PromptSession[str] | None = None
        # By the text up to the call's `(`; (signature, from Home Assistant)
        self._found: dict[str, tuple[Signature | None, bool]] = {}
        self._asking: set[str] = set()
        self._fetches: set[asyncio.Task[None]] = set()

    def attach(self, session: PromptSession[str]) -> None:
        self._session = session
        session.default_buffer.on_text_changed += self._moved
        session.default_buffer.on_cursor_position_changed += self._moved

    def forget(self) -> None:
        self._found.clear()

    def _moved(self, buffer: Buffer) -> None:
        assert self._session is not None
        inside = call_at(buffer.document.text_before_cursor) is not None
        self._session.bottom_toolbar = self.toolbar if inside else None

    def toolbar(self) -> StyleAndTextTuples:
        assert self._session is not None
        return self.render(
            self._session.default_buffer.document.text_before_cursor,
            get_app().output.get_size().columns,
        )

    def render(self, text: str, width: int | None = None) -> StyleAndTextTuples:
        """The toolbar for `text`: nothing yet if Home Assistant hasn't
        answered, the toolbar being redrawn when it does."""
        if (call := call_at(text)) is None:
            return []
        key = text[: call[0] + 1]
        if key not in self._found:
            if self.completer.asks_remotely(key):
                self._ask(key)
                return []
            self._found[key] = (self._local(key), False)
        found, remote = self._found[key]
        if found is None:
            return []
        return _formatted(
            found, found.active(call[1], call[2]), remote=remote, width=width
        )

    def _local(self, key: str) -> Signature | None:
        return signature(key, len(key), self.completer.namespace)

    def _ask(self, key: str) -> None:
        if key in self._asking:
            return
        self._asking.add(key)
        fetch = asyncio.ensure_future(self._fetch(key))
        self._fetches.add(fetch)
        fetch.add_done_callback(self._fetches.discard)

    async def _fetch(self, key: str) -> None:
        live = self.completer.live
        assert live is not None
        try:
            answer = await live.client.call(
                "ha_repl_server/complete",
                kind="signature",
                session=live.session_name,
                source=key,
                cursor=len(key),
            )
        except Exception:  # noqa: BLE001 - remembered, so not asked again on every redraw
            answer = {"signature": None}
        finally:
            self._asking.discard(key)
        if answer["signature"] is not None:
            self._found[key] = (Signature.from_dict(answer["signature"]), True)
        else:
            # Not something Home Assistant could follow: try here instead.
            self._found[key] = (self._local(key), False)
        with contextlib.suppress(Exception):
            get_app().invalidate()


def _formatted(
    found: Signature, active: int | None, *, remote: bool, width: int | None = None
) -> StyleAndTextTuples:
    """One line: wider than `width`, the parameters before the one being
    typed are left out, so it stays in view, and the end is cut."""
    parts: StyleAndTextTuples = [("class:signature.ha", "HA ")] if remote else []
    parts.append(("class:signature", f"{found.name}("))
    kinds = [p.kind for p in found.params]
    first = 0
    if width is not None and active is not None:
        while first < active and _length(found, first, active) > width:
            first += 1
        if first:
            parts.append(("class:signature", "…"))
    for i, param in enumerate(found.params):
        if i < first:
            continue
        if i:
            parts.append(("class:signature", ", "))
        if (
            param.kind == "KEYWORD_ONLY"
            and "VAR_POSITIONAL" not in kinds[:i]
            and (i == 0 or kinds[i - 1] != "KEYWORD_ONLY")
        ):
            parts.append(("class:signature", "*, "))
        style = "class:signature.active" if i == active else "class:signature"
        parts.append((style, param.text))
        if param.kind == "POSITIONAL_ONLY" and (
            i + 1 == len(kinds) or kinds[i + 1] != "POSITIONAL_ONLY"
        ):
            parts.append(("class:signature", ", /"))
    parts.append(("class:signature", ")"))
    if found.returns:
        parts.append(("class:signature", f" -> {found.returns}"))
    if found.doc:
        parts.append(("class:signature.doc", f"  {found.doc}"))
    if width is not None:
        parts = _cut(parts, width)
    return parts


def _length(found: Signature, first: int, active: int) -> int:
    """How far the line runs to the end of the active parameter, starting
    at parameter `first`."""
    shown = found.params[first : active + 1]
    return len(found.name) + 6 + sum(len(p.text) + 4 for p in shown)


def _cut(parts: StyleAndTextTuples, width: int) -> StyleAndTextTuples:
    cut: StyleAndTextTuples = []
    room = width - 1
    for fragment in parts:
        style, text = fragment[0], fragment[1]
        if len(text) > room:
            cut.append((style, text[: max(room - 1, 0)] + "…"))
            break
        cut.append((style, text))
        room -= len(text)
    return cut


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
