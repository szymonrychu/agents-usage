"""A small server-sent events parser (pure): lines in, events out.

Only what the orchestrator stream needs: `event:`, `data:` (several lines joined by a newline),
`id:`, comments and the blank line that ends an event. Anything else (`retry:`, a line without a
colon) is skipped. The fields of one event never leak into the next, `id` included.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

DEFAULT_EVENT = "message"


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str
    id: str | None = None


class SseParser:
    """Feed it one line at a time (without its line ending); it returns an event when a blank
    line completes one that has data."""

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []
        self._id: str | None = None

    def feed(self, line: str) -> SseEvent | None:
        line = line.removesuffix("\r")
        if not line:
            event = None
            if self._data:
                event = SseEvent(self._event or DEFAULT_EVENT, "\n".join(self._data), self._id)
            self._reset()
            return event
        field, colon, value = line.partition(":")
        if not colon or not field:
            return None  # a comment (empty field name) or a line that is no field
        value = value.removeprefix(" ")
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        elif field == "id":
            self._id = value
        return None


def parse_sse(lines: Iterable[str]) -> Iterator[SseEvent]:
    """The events in `lines`; an event the stream ends in the middle of is not dispatched."""
    parser = SseParser()
    for line in lines:
        event = parser.feed(line)
        if event is not None:
            yield event
