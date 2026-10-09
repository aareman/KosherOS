"""Checked browser streams for ChatGPT snapshots/delta-v1 and Claude SSE."""

import json
from typing import Literal, final

from .ai_delta import Channels
from .ai_json import ContentFilter, mapping, parse
from .ai_sse import MAX_EVENT_BYTES, Decoder, Event
from .ai_text import OutputBlocked, TextStream


@final
class BrowserStream:
    """Own framing, provider state and withheld text for a single generation."""

    def __init__(self, provider: Literal["chatgpt", "claude"], filters: ContentFilter) -> None:
        self.provider = provider
        self.filters = filters
        self.decoder = Decoder()
        self.channels = Channels()
        self.text: dict[int, TextStream] = {}
        self.fields: dict[int, str] = {}
        self.inputs: dict[int, str] = {}
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
            if self.inputs:
                raise OutputBlocked("AI tool input ended inside a content block")
            self.ended = True
            final = bytearray(self._flush())
            if self.legacy is not None:
                final.extend(Event(()).encode(json.dumps(self.filters.clean(self.legacy))))
            for channel, document in self.channels.documents.items():
                value = {"c": channel, "p": "", "o": "replace", "v": self.filters.clean(document)}
                final.extend(Event((b"event: delta",)).encode(json.dumps(value)))
            return bytes(final) + event.encode()
        value = parse(payload)
        if event.name == "delta_encoding":
            if self.provider != "chatgpt" or value != "v1":
                raise OutputBlocked("Unsupported AI stream encoding")
            self.channels = Channels()
            return event.encode()
        if self.provider == "chatgpt":
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
        return self._claude(event, mapping(value))

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

    def _claude(self, event: Event, data: dict) -> bytes:
        kind = data.get("type", event.name)
        index = data.get("index", 0)
        if not isinstance(index, int) or index < 0 or index > 1024:
            raise OutputBlocked("Invalid Claude content block index")
        if kind in {"content_block_start", "content_block_delta"}:
            key = "content_block" if kind == "content_block_start" else "delta"
            block = mapping(data.get(key))
            if block.get("type") == "tool_use":
                self.filters.tool(block)
            if block.get("type") == "input_json_delta":
                part = block.get("partial_json")
                if not isinstance(part, str) or len(self.inputs) >= 128:
                    raise OutputBlocked("Unsupported Claude tool input")
                pending = self.inputs.get(index, "") + part
                if len(pending) > 1_000_000:
                    raise OutputBlocked("Claude tool input exceeds the inspection limit")
                self.inputs[index] = pending
                data[key] = block | {"partial_json": ""}
                return event.encode(json.dumps(data))
            field = "thinking" if block.get("type") in {"thinking", "thinking_delta"} else "text"
            value = block.get(field)
            if isinstance(value, str):
                if index not in self.text and len(self.text) >= 128:
                    raise OutputBlocked("Too many Claude output blocks")
                stream = self.text.setdefault(index, TextStream(self.filters.text, self.filters.string))
                self.fields[index] = field
                replacement = stream.push(value)
                data[key] = block | {field: replacement}
                return event.encode(json.dumps(self.filters.clean(data)))
        prefix = b""
        if kind == "content_block_stop":
            prefix = self._flush(index)
            if index in self.inputs:
                pending = self.inputs.pop(index)
                self.filters.tool(parse(pending))
                delta = {"type": "content_block_delta", "index": index,
                         "delta": {"type": "input_json_delta", "partial_json": pending}}
                prefix += Event((b"event: content_block_delta",)).encode(json.dumps(delta))
        if kind == "message_stop":
            if self.inputs:
                raise OutputBlocked("Claude tool input ended inside a content block")
            prefix = self._flush()
            self.ended = True
        if "completion" in data:
            value = data["completion"]
            if not isinstance(value, str):
                raise OutputBlocked("Invalid Claude completion")
            stream = self.text.setdefault(0, TextStream(self.filters.text, self.filters.string))
            self.fields[0] = "completion"
            data["completion"] = stream.push(value)
            if data.get("stop_reason"):
                data["completion"] += stream.finish()
                del self.text[0]
                self.ended = True
        return prefix + event.encode(json.dumps(self.filters.clean(data)))

    def _flush(self, index: int | None = None) -> bytes:
        output = bytearray()
        indices = list(self.text) if index is None else [index]
        for current in indices:
            stream = self.text.pop(current, None)
            if stream is None:
                continue
            field = self.fields.pop(current)
            text = stream.finish()
            if text:
                if field == "completion":
                    value = {"completion": text}
                    name = "completion"
                else:
                    value = {"type": "content_block_delta", "index": current,
                             "delta": {"type": field + "_delta", field: text}}
                    name = "content_block_delta"
                output.extend(Event((f"event: {name}".encode(),)).encode(json.dumps(value)))
        return bytes(output)
