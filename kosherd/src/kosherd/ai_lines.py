"""ChatGPT's conversation stream: one JSON object per line.

Observed on chatgpt.com in October 2026 (content type
application/vnd.openai.conversation-source+ndjson). An answer arrives as a
"content" line that replaces the message text, then "content" lines that
append to it. Every streaming line also carries a full snapshot of the text
so far in a "dil" table (the renderer's constants) with a markdown fallback;
the completion line carries no text at all. Around them come "control",
"state" and "shared" lines; the last "shared" line holds the whole finished
answer again, as HTML.

Text released early cannot be taken back, so the markdown stream and the
snapshot table both hold back an unfinished tail while the status is
"streaming", and the finished snapshot is attached to the completion line.
"""

from collections.abc import Callable
from typing import Final, final

from .ai_json import ContentFilter, dumps, mapping, parse
from .ai_sse import MAX_EVENT_BYTES, InvalidEventStream
from .ai_text import OutputBlocked, TextStream

BLOCKED_TEXT: Final = ("This answer was not shown. "
                       "It did not pass the filter set for this account.")


@final
class LineDecoder:
    """Complete lines only, whatever the network's chunking."""

    def __init__(self, limit: int = MAX_EVENT_BYTES) -> None:
        self.limit = limit
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        if len(self._buffer) > self.limit:
            raise InvalidEventStream("AI line exceeds the inspection limit")
        lines: list[bytes] = []
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                return lines
            lines.append(bytes(self._buffer[:index]))
            del self._buffer[:index + 1]

    def finish(self) -> list[bytes]:
        """The last line may legitimately lack its newline."""
        rest = bytes(self._buffer)
        self._buffer.clear()
        return [rest] if rest.strip() else []


@final
class LineStream:
    """Checked output for one conversation stream."""

    def __init__(self, filters: ContentFilter,
                 on_block: Callable[[OutputBlocked], None] | None = None) -> None:
        self.filters = filters
        self.on_block = on_block or (lambda error: None)
        self.decoder = LineDecoder()
        self.markdown: dict[str, TextStream] = {}
        self.snapshots: dict[str, dict] = {}
        self.blocked: set[str] = set()
        self.lines = 0

    def feed(self, chunk: bytes) -> bytes:
        return b"".join(self.line(raw) for raw in self.decoder.feed(chunk))

    def finish(self) -> bytes:
        return b"".join(self.line(raw) for raw in self.decoder.finish())

    def line(self, raw: bytes) -> bytes:
        if not raw.strip():
            return raw + b"\n"
        self.lines += 1
        if self.lines > 100_000:
            raise OutputBlocked("AI stream exceeds the line limit")
        data = mapping(parse(raw))
        if data.get("type") == "content":
            return self._content(data)
        try:
            cleaned = self.filters.clean(data)
        except OutputBlocked as error:
            # A template or state record the page needs in order to finish,
            # carrying text it may not show: keep the record, drop the text.
            self.on_block(error)
            cleaned = self.filters.blank(data)
        return dumps(cleaned).encode() + b"\n"

    def _content(self, data: dict) -> bytes:
        message = str(data.get("messageId", ""))
        streaming = data.get("status") == "streaming"
        if message in self.blocked:
            return self._notice(data, first=False)
        try:
            markdown = data.get("markdown")
            if not isinstance(markdown, str):
                raise OutputBlocked("AI content line without text")
            stream = self.markdown.get(message)
            if stream is None or data.get("mode") == "replace":
                stream = TextStream(self.filters.text, self.filters.string)
                self.markdown[message] = stream
            released = stream.push(markdown)
            snapshot = data.get("dil")
            if isinstance(snapshot, dict):
                full, held = self._snapshot(snapshot, streaming)
                self.snapshots[message] = full
                snapshot = held
            if not streaming:
                released += stream.finish()
                del self.markdown[message]
                if snapshot is None:
                    snapshot = self.snapshots.get(message)
                self.snapshots.pop(message, None)
            rest = {key: value for key, value in data.items() if key not in {"markdown", "dil"}}
            cleaned = self.filters.clean(rest)
            cleaned["markdown"] = released
            if snapshot is not None:
                cleaned["dil"] = snapshot
        except OutputBlocked as error:
            self.blocked.add(message)
            self.markdown.pop(message, None)
            self.snapshots.pop(message, None)
            self.on_block(error)
            return self._notice(data, first=True)
        return dumps(cleaned).encode() + b"\n"

    def _snapshot(self, snapshot: dict, streaming: bool) -> tuple[dict, dict]:
        """The renderer's snapshot, checked: (complete, with the tail held back)."""
        full: dict = {}
        held: dict = {}
        for key, value in snapshot.items():
            if key == "constants" and isinstance(value, dict):
                checked = {name: self.filters.string(item) if isinstance(item, str)
                           else self.filters.clean(item) for name, item in value.items()}
                full[key] = checked
                held[key] = dict(checked)
                if streaming and checked:
                    last = next(reversed(checked))
                    if isinstance(checked[last], str):
                        held[key][last] = self.filters.text.withhold(checked[last])
            elif key == "fallbackMarkdown" and isinstance(value, str):
                checked = self.filters.string(value)
                full[key] = checked
                held[key] = self.filters.text.withhold(checked) if streaming else checked
            else:
                full[key] = held[key] = self.filters.clean(value, key)
        return full, held

    def _notice(self, data: dict, *, first: bool) -> bytes:
        """The line as the page expects it, with the notice as its only prose.

        The page stops with "Unable to connect" when a streaming line
        arrives without its renderer snapshot or its reference lists, so
        the shape of the line is kept and only the words change. The
        snapshot shows the notice; later snapshots keep showing it."""
        line = self.filters.blank(data)
        line["markdown"] = BLOCKED_TEXT if first else ""
        snapshot = line.get("dil")
        original = data.get("dil")
        if isinstance(snapshot, dict) and isinstance(original, dict):
            if isinstance(original.get("code"), str):
                snapshot["code"] = original["code"]  # the renderer's own program, not prose
            constants = snapshot.get("constants")
            if isinstance(constants, dict) and constants:
                constants[next(iter(constants))] = BLOCKED_TEXT
            if "fallbackMarkdown" in snapshot:
                snapshot["fallbackMarkdown"] = BLOCKED_TEXT
        return dumps(line).encode() + b"\n"
