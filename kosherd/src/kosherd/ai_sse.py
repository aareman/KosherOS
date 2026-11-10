"""Bounded SSE framing independent of TCP chunks and UTF-8 boundaries."""

from dataclasses import dataclass
from typing import Final, final

MAX_EVENT_BYTES: Final = 16 * 1024 * 1024


class InvalidEventStream(ValueError):
    """The upstream stream cannot be safely framed."""


@dataclass(frozen=True, slots=True)
class Event:
    """One complete event, retaining fields the client uses for resumption."""

    lines: tuple[bytes, ...]

    @property
    def name(self) -> str:
        names = [line.partition(b":")[2].removeprefix(b" ")
                 for line in self.lines if line.partition(b":")[0] == b"event"]
        return names[-1].decode("utf-8") if names else "message"

    @property
    def data(self) -> str | None:
        values: list[bytes] = []
        for line in self.lines:
            field, separator, value = line.partition(b":")
            if field == b"data":
                values.append(value.removeprefix(b" ") if separator else b"")
        if not values:
            return None
        return b"\n".join(values).decode("utf-8", errors="strict")

    def encode(self, data: str | None = None) -> bytes:
        """Replace data while preserving event names, IDs, retries and comments."""
        if data is None:
            return b"\n".join(self.lines) + b"\n\n"
        lines = [line for line in self.lines if line.partition(b":")[0] != b"data"]
        lines.extend(b"data: " + part.encode("utf-8") for part in data.split("\n"))
        return b"\n".join(lines) + b"\n\n"


@final
class Decoder:
    """Accumulate incomplete lines/events; never release a partial event."""

    def __init__(self, limit: int = MAX_EVENT_BYTES) -> None:
        self.limit = limit
        self._line = bytearray()
        self._lines: list[bytes] = []
        self._size = 0
        self._after_cr = False
        self._prefix = b""
        self._started = False

    def feed(self, chunk: bytes) -> list[Event]:
        """Accept arbitrary wire fragments, including CR/LF split across calls."""
        data = chunk
        if not self._started:
            data = self._prefix + data
            if len(data) < 3 and b"\xef\xbb\xbf".startswith(data):
                self._prefix = data
                return []
            data = data.removeprefix(b"\xef\xbb\xbf")
            self._prefix = b""
            self._started = True
        events: list[Event] = []
        for byte in data:
            if self._after_cr and byte == 10:
                self._after_cr = False
                continue
            self._after_cr = byte == 13
            self._size += 1
            if self._size > self.limit:
                raise InvalidEventStream("SSE event exceeds the inspection limit")
            if byte not in (10, 13):
                self._line.append(byte)
                continue
            if self._line:
                self._lines.append(bytes(self._line))
                self._line.clear()
            else:
                if self._lines:
                    events.append(Event(tuple(self._lines)))
                self._lines.clear()
                self._size = 0
        return events

    def finish(self) -> None:
        """An unterminated event is not safe to forward at EOF."""
        if self._prefix or self._line or self._lines:
            raise InvalidEventStream("SSE stream ended inside an event")
