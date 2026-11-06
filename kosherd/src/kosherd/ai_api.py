"""Model output as events, whatever the API speaks.

Four shapes cover the tools and sites a family meets: Anthropic Messages
(Claude Code, the Claude desktop and web apps), OpenAI Responses (Codex,
and the ChatGPT backend it talks to), OpenAI Chat Completions (most other
APIs copy it: xAI, Mistral, DeepSeek, Groq, OpenRouter, Together) and
Gemini. Each event is recognised by its shape, so one handler serves every
host and a host that serves two shapes.

Text deltas are joined per output stream and released with a held-back tail,
tool arguments are held until complete and refused rather than rewritten,
and the full-text copies the APIs send at the end are checked whole.
"""

import json
from collections.abc import Callable
from typing import Final, final

from .ai_generic import GenericText
from .ai_json import ContentFilter, dumps, mapping, parse
from .ai_lines import LineDecoder
from .ai_sse import Decoder, Event
from .ai_text import OutputBlocked, TextStream

ANTHROPIC: Final = frozenset({
    "message_start", "content_block_start", "content_block_delta", "content_block_stop",
    "message_delta", "message_stop", "ping", "error", "completion",
})
# Responses API text streams: the event prefix, and the field its ".done" carries.
RESPONSES_TEXT: Final = {
    "response.output_text": "text", "response.reasoning_summary_text": "text",
    "response.reasoning_text": "text", "response.refusal": "refusal",
}
RESPONSES_ARGUMENTS: Final = {
    "response.function_call_arguments": "arguments", "response.custom_tool_call_input": "input",
}
STREAM_KEYS: Final = ("item_id", "output_index", "content_index", "summary_index")
MAX_STREAMS: Final = 256
MAX_INPUT: Final = 1_000_000

Template = Callable[[str], dict]


def _arguments(value: str):
    """Tool arguments as JSON when they are JSON, otherwise as the text they are."""
    try:
        return json.loads(value)
    except ValueError:
        return value


@final
class ApiEvents:
    """One generation (or one socket): the events to send for each event received."""

    def __init__(self, filters: ContentFilter) -> None:
        self.filters = filters
        self.generic = GenericText(filters)
        self.streams: dict[tuple, tuple[TextStream, Template]] = {}
        self.inputs: dict[tuple, str] = {}
        self.ended = False

    def event(self, data: dict) -> list[dict]:
        kind = data.get("type")
        if isinstance(kind, str):
            if kind in ANTHROPIC:
                return self._anthropic(data)
            if kind.startswith("response."):
                return self._responses(data)
        if isinstance(data.get("choices"), list):
            return self._chat(data)
        if isinstance(data.get("candidates"), list):
            return self._gemini(data)
        # A shape nobody taught us: every string that reads as prose is checked.
        return [self.generic.clean(data)]

    def finish(self) -> list[dict]:
        """The stream ended: release what was held back, once it is checked."""
        if self.inputs:
            raise OutputBlocked("AI tool arguments ended before they were complete")
        return self._flush(list(self.streams))

    # ---- shared ------------------------------------------------------------

    def _stream(self, key: tuple, template: Template) -> TextStream:
        found = self.streams.get(key)
        if found is not None:
            return found[0]
        if len(self.streams) >= MAX_STREAMS:
            raise OutputBlocked("Too many AI output streams")
        stream = TextStream(self.filters.text, self.filters.string)
        self.streams[key] = (stream, template)
        return stream

    def _flush(self, keys: list[tuple]) -> list[dict]:
        out = []
        for key in keys:
            found = self.streams.pop(key, None)
            if found is None:
                continue
            stream, template = found
            text = stream.finish()
            if text:
                out.append(template(text))
        return out

    def _hold(self, key: tuple, part) -> None:
        if not isinstance(part, str) or len(self.inputs) >= 128:
            raise OutputBlocked("Unsupported AI tool arguments")
        pending = self.inputs.get(key, "") + part
        if len(pending) > MAX_INPUT:
            raise OutputBlocked("AI tool arguments exceed the inspection limit")
        self.inputs[key] = pending

    # ---- Anthropic Messages --------------------------------------------------

    def _anthropic(self, data: dict) -> list[dict]:
        kind = data["type"]
        if kind == "completion":
            value = data.get("completion")
            if not isinstance(value, str):
                raise OutputBlocked("Invalid completion")
            stream = self._stream(("completion",), lambda t: {"type": "completion", "completion": t})
            data["completion"] = stream.push(value)
            if data.get("stop_reason"):
                data["completion"] += stream.finish()
                del self.streams[("completion",)]
                self.ended = True
            return [data]
        index = data.get("index", 0)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index > 1024:
            raise OutputBlocked("Invalid content block index")
        key = ("anthropic", index)
        if kind in {"content_block_start", "content_block_delta"}:
            field = "content_block" if kind == "content_block_start" else "delta"
            block = mapping(data.get(field))
            block_type = block.get("type")
            if block_type in {"tool_use", "server_tool_use"}:
                self.filters.tool(block)
            if block_type == "input_json_delta":
                self._hold(key, block.get("partial_json"))
                data[field] = block | {"partial_json": ""}
                return [data]
            text_field = "thinking" if block_type in {"thinking", "thinking_delta"} else "text"
            value = block.get(text_field)
            if isinstance(value, str):
                stream = self._stream(key, lambda t, i=index, f=text_field: {
                    "type": "content_block_delta", "index": i, "delta": {"type": f + "_delta", f: t}})
                data[field] = block | {text_field: stream.push(value)}
                return [data]
            return [self.filters.clean(data)]
        out: list[dict] = []
        if kind == "content_block_stop":
            out += self._flush([key])
            pending = self.inputs.pop(key, None)
            if pending is not None:
                self.filters.tool(parse(pending))
                out.append({"type": "content_block_delta", "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": pending}})
        if kind == "message_stop":
            if self.inputs:
                raise OutputBlocked("AI tool input ended inside a content block")
            out += self._flush(list(self.streams))
            self.ended = True
        out.append(self.filters.clean(data))
        return out

    # ---- OpenAI Responses ----------------------------------------------------

    def _responses(self, data: dict) -> list[dict]:
        kind = data["type"]
        base = {name: data[name] for name in STREAM_KEYS if name in data}
        for prefix, field in RESPONSES_TEXT.items():
            key = ("responses", prefix, *(data.get(name) for name in STREAM_KEYS))
            if kind == prefix + ".delta":
                delta = data.get("delta")
                if not isinstance(delta, str):
                    raise OutputBlocked("Invalid AI text delta")
                stream = self._stream(key, lambda t, b=base, k=kind: b | {"type": k, "delta": t})
                data["delta"] = stream.push(delta)
                return [data]
            if kind == prefix + ".done":
                out = self._flush([key])
                value = data.get(field)
                if isinstance(value, str):
                    data[field] = self.filters.string(value)
                return out + [data]
        for prefix, field in RESPONSES_ARGUMENTS.items():
            key = ("arguments", prefix, data.get("item_id"), data.get("output_index"))
            if kind == prefix + ".delta":
                self._hold(key, data.get("delta"))
                data["delta"] = ""
                return [data]
            if kind == prefix + ".done":
                self.inputs.pop(key, None)
                value = data.get(field)
                if not isinstance(value, str):
                    return [data]
                self.filters.tool(_arguments(value))
                return [base | {"type": prefix + ".delta", "delta": value}, data]
        response = data.get("response")
        if isinstance(response, dict):
            # The response object echoes the request (instructions, input):
            # those are the person's own words. Only the model's output is ours.
            output = response.get("output")
            if output is not None:
                response["output"] = self.filters.clean(output, "output")
            if kind in {"response.completed", "response.failed", "response.incomplete"}:
                self.ended = True
            return [data]
        return [self.filters.clean(data)]

    # ---- OpenAI Chat Completions ---------------------------------------------

    def _chat(self, data: dict) -> list[dict]:
        out: list[dict] = []
        for choice in data["choices"]:
            choice = mapping(choice)
            index = choice.get("index", 0)
            delta = choice.get("delta")
            if isinstance(delta, dict):
                for field in ("content", "reasoning_content", "reasoning"):
                    value = delta.get(field)
                    if isinstance(value, str):
                        stream = self._stream(("chat", index, field), lambda t, i=index, f=field, d=data: {
                            "id": d.get("id"), "object": "chat.completion.chunk",
                            "choices": [{"index": i, "delta": {f: t}, "finish_reason": None}]})
                        delta[field] = stream.push(value)
                calls = delta.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        call = mapping(call)
                        function = call.get("function")
                        if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                            self._hold(("chat", index, "call", call.get("index", 0)), function["arguments"])
                            function["arguments"] = ""
            if isinstance(choice.get("message"), dict):
                choice["message"] = self.filters.clean(choice["message"], "message")
            if choice.get("finish_reason"):
                out += self._flush([key for key in self.streams if key[:2] == ("chat", index)])
                for key in [key for key in self.inputs if key[:3] == ("chat", index, "call")]:
                    arguments = self.inputs.pop(key)
                    self.filters.tool(_arguments(arguments))
                    out.append({"id": data.get("id"), "object": "chat.completion.chunk", "choices": [{
                        "index": index, "finish_reason": None,
                        "delta": {"tool_calls": [{"index": key[3], "function": {"arguments": arguments}}]}}]})
        return out + [data]

    # ---- Gemini ----------------------------------------------------------------

    def _gemini(self, data: dict) -> list[dict]:
        for candidate in data["candidates"]:
            candidate = mapping(candidate)
            index = candidate.get("index", 0)
            content = candidate.get("content")
            if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
                continue
            kept: list[dict] = []
            text = ""
            for part in content["parts"]:
                part = mapping(part)
                if isinstance(part.get("text"), str):
                    text += part["text"]
                else:
                    if "functionCall" in part:
                        self.filters.tool(part)
                    kept.append(self.filters.clean(part))
            key = ("gemini", index)
            stream = self._stream(key, lambda t, i=index: {
                "candidates": [{"index": i, "content": {"role": "model", "parts": [{"text": t}]}}]})
            released = stream.push(text)
            if candidate.get("finishReason"):
                released += stream.finish()
                self.streams.pop(key, None)
            content["parts"] = kept + [{"text": released}]
        return [data]


@final
class ApiStream:
    """The events of one HTTP event stream, checked.

    `lenient` is for a host nobody listed: an event whose data is not JSON
    is treated as the prose it probably is, rather than refused.
    """

    def __init__(self, filters: ContentFilter, lenient: bool = False) -> None:
        self.api = ApiEvents(filters)
        self.lenient = lenient
        self.decoder = Decoder()
        self.named = False
        self.events = 0

    def feed(self, chunk: bytes) -> bytes:
        output = bytearray()
        for event in self.decoder.feed(chunk):
            self.events += 1
            if self.events > 100_000:
                raise OutputBlocked("AI stream exceeds the event limit")
            output.extend(self.event(event))
        return bytes(output)

    def finish(self) -> bytes:
        self.decoder.finish()
        return b"".join(self.encode(item) for item in self.api.finish())

    def event(self, event: Event) -> bytes:
        payload = event.data
        if payload is None:
            return event.encode()
        if payload.strip() == "[DONE]":
            return b"".join(self.encode(item) for item in self.api.finish()) + event.encode()
        self.named = event.name != "message"
        try:
            document = parse(payload)
        except OutputBlocked:
            if not self.lenient:
                raise
            return event.encode(self.api.generic.text(payload))
        if not isinstance(document, dict):
            if not self.lenient:
                raise OutputBlocked("Unsupported AI response structure")
            return event.encode(dumps(self.api.generic.clean(document)))
        outputs = self.api.event(document)
        extra = b"".join(self.encode(item) for item in outputs[:-1])
        return extra + event.encode(dumps(outputs[-1]))

    def encode(self, data: dict) -> bytes:
        kind = data.get("type")
        lines = (f"event: {kind}".encode(),) if self.named and isinstance(kind, str) else ()
        return Event(lines).encode(dumps(data))


@final
class ApiLines:
    """One JSON object per line, from a host with no page protocol of its own."""

    def __init__(self, filters: ContentFilter, lenient: bool = False) -> None:
        self.api = ApiEvents(filters)
        self.lenient = lenient
        self.decoder = LineDecoder()
        self.lines = 0

    def feed(self, chunk: bytes) -> bytes:
        return b"".join(self.line(raw) for raw in self.decoder.feed(chunk))

    def finish(self) -> bytes:
        output = b"".join(self.line(raw) for raw in self.decoder.finish())
        return output + b"".join(dumps(item).encode() + b"\n" for item in self.api.finish())

    def line(self, raw: bytes) -> bytes:
        if not raw.strip():
            return raw + b"\n"
        self.lines += 1
        if self.lines > 100_000:
            raise OutputBlocked("AI stream exceeds the line limit")
        try:
            document = parse(raw)
        except OutputBlocked:
            if not self.lenient:
                raise
            return self.api.generic.text(raw.decode("utf-8", "replace")).encode() + b"\n"
        if not isinstance(document, dict):
            if not self.lenient:
                raise OutputBlocked("Unsupported AI response structure")
            return dumps(self.api.generic.clean(document)).encode() + b"\n"
        return b"".join(dumps(item).encode() + b"\n" for item in self.api.event(document))
