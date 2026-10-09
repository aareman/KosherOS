"""ChatGPT WebSocket envelopes carrying SSE, isolated by topic."""

from typing import final

from .ai_browser import BrowserStream
from .ai_json import ContentFilter, dumps, mapping, parse
from .ai_sse import MAX_EVENT_BYTES
from .ai_text import OutputBlocked


@final
class SocketFilter:
    """Maintain bounded response state while a socket multiplexes generations."""

    def __init__(self, filters: ContentFilter) -> None:
        self.filters = filters
        self.topics: dict[str, BrowserStream] = {}

    def feed(self, content: bytes) -> bytes:
        if len(content) > MAX_EVENT_BYTES:
            raise OutputBlocked("AI socket frame exceeds the inspection limit")
        document = parse(content)
        if not isinstance(document, list):
            raise OutputBlocked("Unsupported AI socket envelope")
        output = []
        for value in document:
            envelope = mapping(value)
            outer = envelope.get("payload")
            if isinstance(outer, dict) and isinstance(outer.get("payload"), dict):
                inner = mapping(outer["payload"])
                encoded = inner.get("encoded_item")
                if isinstance(encoded, str):
                    topic = envelope.get("topic_id")
                    if not isinstance(topic, str) or not topic or len(topic) > 512:
                        raise OutputBlocked("AI socket generation has no bounded topic ID")
                    if topic not in self.topics and len(self.topics) >= 32:
                        raise OutputBlocked("Too many concurrent AI socket topics")
                    stream = self.topics.setdefault(topic, BrowserStream("chatgpt", self.filters))
                    checked = stream.feed(encoded.encode()).decode()
                    stream.decoder.finish()
                    inner["encoded_item"] = checked
                    if stream.ended:
                        del self.topics[topic]
                    output.append(envelope)
                    continue
            # Metadata without encoded events still cannot carry unchecked text.
            output.append(self.filters.clean(envelope))
        return dumps(output).encode()
