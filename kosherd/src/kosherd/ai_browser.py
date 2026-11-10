"""ChatGPT's event stream in the browser: message snapshots and delta-v1 patches."""

import json
from typing import final

from .ai_delta import Channels
from .ai_json import ContentFilter, mapping, parse
from .ai_sse import MAX_EVENT_BYTES, Decoder, Event
from .ai_text import OutputBlocked


@final
class BrowserStream:
    """Own framing, channel state and withheld text for a single generation."""

    def __init__(self, filters: ContentFilter) -> None:
        self.filters = filters
        self.decoder = Decoder()
        self.channels = Channels()
        self.legacy = None
        self.ended = False
        self.events = 0

    def feed(self, chunk: bytes) -> bytes:
        result = bytearray()
        for event in self.decoder.feed(chunk):
            self.events += 1
            if self.events > 100_000:
                raise OutputBlocked("AI stream exceeds the event limit")
            result.extend(self.event(event))
        return bytes(result)

    def finish(self) -> bytes:
        self.decoder.finish()
        if not self.ended:
            raise OutputBlocked("AI generation ended without a completion marker")
        return b""

    def event(self, event: Event) -> bytes:
        payload = event.data
        if payload is None:
            return event.encode()
        if self.ended:
            raise OutputBlocked("AI content arrived after completion")
        if payload == "[DONE]":
            self.ended = True
            final = bytearray()
            if self.legacy is not None:
                final.extend(Event(()).encode(json.dumps(self.filters.clean(self.legacy))))
            for channel, document in self.channels.documents.items():
                value = {"c": channel, "p": "", "o": "replace", "v": self.filters.clean(document)}
                final.extend(Event((b"event: delta",)).encode(json.dumps(value)))
            return bytes(final) + event.encode()
        value = parse(payload)
        if event.name == "delta_encoding":
            if value != "v1":
                raise OutputBlocked("Unsupported AI stream encoding")
            self.channels = Channels()
            return event.encode()
        if event.name == "delta":
            channel, document = self.channels.update(value)
            if len(json.dumps(document)) > MAX_EVENT_BYTES:
                raise OutputBlocked("AI document exceeds the inspection limit")
            cleaned = self._snapshot(document)
            # Explicit headers avoid inheriting a path from the original wire.
            return event.encode(json.dumps({"c": channel, "p": "", "o": "replace", "v": cleaned}))
        if isinstance(value, dict) and isinstance(value.get("message"), dict):
            self.legacy = value
        return event.encode(json.dumps(self._snapshot(value)))

    def _snapshot(self, document):
        cleaned = self.filters.clean(document)
        if not isinstance(cleaned, dict):
            return cleaned
        message = cleaned.get("message")
        if not isinstance(message, dict):
            return cleaned
        author = message.get("author")
        if isinstance(author, dict) and author.get("role") == "user":
            return cleaned
        content = message.get("content")
        if not isinstance(content, dict):
            return cleaned
        parts = content.get("parts")
        if not isinstance(parts, list):
            return cleaned
        if message.get("status") in {"finished_successfully", "finished_partial"}:
            return cleaned
        retained = []
        for part in parts:
            if isinstance(part, str):
                boundary = part.rfind(" ", 0, max(0, len(part) - self.filters.text.lookahead))
                retained.append(part[:boundary + 1] if boundary >= 0 else "")
            else:
                retained.append(part)
        content["parts"] = retained
        return cleaned
