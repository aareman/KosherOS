"""mitmproxy addon: AI answers in the browser pass the account's filter.

Runs after the main KosherOS addon and reuses its policy, word list, scorer
and picture detector. It touches only the responses that carry an answer:
ChatGPT's line-by-line conversation stream, ChatGPT's and Claude's event
streams, and the JSON of a conversation being opened. Everything else on
those sites (scripts, the anti-bot handshake, redirects, empty replies) goes
through untouched, because an AI site whose plumbing is broken is not
filtered, it is down.

A refusal never forwards what it refused: the body callback drops the
upstream bytes and sends a notice instead.
"""

from __future__ import annotations

import json
import logging
import zlib
from collections.abc import Callable
from typing import Final, Literal, Protocol, final

from mitmproxy import ctx, http

from . import activity, content, language, siterules, vision
from .ai_api import ApiStream
from .ai_browser import BrowserStream
from .ai_images import ImageFilter
from .ai_json import ContentFilter, dumps, parse
from .ai_lines import BLOCKED_TEXT, LineStream
from .ai_socket import SocketFilter
from .ai_sse import MAX_EVENT_BYTES, InvalidEventStream
from .ai_text import OutputBlocked, TextFilter, TextPolicy

log = logging.getLogger("kosher-filter")

Provider = Literal["chatgpt", "claude", "api"]
Kind = Literal["events", "lines", "json"]
REFUSED: Final = "refused"

ERROR: Final = json.dumps({"type": "error", "error": {
    "type": "kosher_content_blocked", "message": BLOCKED_TEXT}}).encode()

# The same table the main addon uses for pages: which content level refuses
# an answer, by the account's picture level.
CONTENT_TOLERANCE: Final = {
    "none": content.NSFW, "nsfw": content.NSFW, "suggestive": content.SUGGESTIVE,
    "immodest": content.IMMODEST, "people": content.IMMODEST, "all": content.IMMODEST,
}
# Requests for these need no inspection, so they may stay compressed.
ASSET_SUFFIXES: Final = (".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
                         ".webp", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf",
                         ".mp3", ".mp4", ".webm", ".wasm", ".json")
DROPPED_HEADERS: Final = ("content-length", "content-encoding", "etag", "content-md5", "digest")


class AccountPolicy(Protocol):
    def media_level_for(self, uid: int | None) -> str: ...
    def language_filter_for(self, uid: int | None) -> str: ...
    def blocked_categories_for(self, uid: int | None) -> list: ...


class FilterOwner(Protocol):
    policy: AccountPolicy
    wordlist: language.Wordlist
    scorer: content.Scorer
    vision: vision.ImageFilter


# The hosts whose answers are read, by the shape of what they send. "api"
# is every API host a tool such as Claude Code or Codex talks to; which API
# shape a host speaks is recognised event by event (ai_api).
SITES: Final = {
    "chatgpt": ("chatgpt.com", "chat.openai.com"),
    "claude": ("claude.ai",),
    "api": ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com",
            "api.x.ai", "api.mistral.ai", "api.deepseek.com", "api.groq.com", "openrouter.ai",
            "api.together.xyz", "api.perplexity.ai", "api.fireworks.ai", "api.cerebras.ai"),
}


def provider(host: str) -> Provider | None:
    host = host.lower().rstrip(".")
    for name, sites in SITES.items():
        if any(host == site or host.endswith("." + site) for site in sites):
            return name
    return None


def header(headers: http.Headers, name: str) -> str:
    return next(iter(headers.get_all(name)), "")


def mime_of(headers: http.Headers) -> str:
    return header(headers, "content-type").split(";", 1)[0].strip().lower()


def is_asset(path: str) -> bool:
    return path.split("?", 1)[0].lower().endswith(ASSET_SUFFIXES)


def body_kind(selected: Provider, flow: http.HTTPFlow) -> Kind | None:
    """Which of the answer-carrying shapes this response is, if any."""
    response = flow.response
    if response is None or response.status_code == 101 or response.status_code >= 300:
        return None
    mime = mime_of(response.headers)
    if mime == "text/event-stream":
        return "events"
    if mime.endswith("+ndjson") or mime in {"application/x-ndjson", "application/jsonl"}:
        return "lines" if selected == "chatgpt" else None
    if mime == "application/json":
        path = flow.request.path.split("?", 1)[0]
        if selected == "api":
            return "json"
        if selected == "chatgpt" and ("/conversation" in path or "/codex/" in path):
            return "json"
        if selected == "claude" and "/chat_conversations" in path:
            return "json"
    return None


def api_shaped(selected: Provider, flow: http.HTTPFlow) -> bool:
    """Does this event stream speak an API (ai_api) rather than ChatGPT's page protocol?"""
    return selected != "chatgpt" or "/codex/" in flow.request.path.split("?", 1)[0]


class Decoder:
    """Streaming decompression, for a server that ignored Accept-Encoding."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding.strip().lower()
        self._feed: Callable[[bytes], bytes]
        self._flush: Callable[[], bytes]
        if self.encoding in {"", "identity"}:
            self._feed, self._flush = (lambda data: data), (lambda: b"")
        elif self.encoding in {"gzip", "x-gzip", "deflate"}:
            inflater = zlib.decompressobj(zlib.MAX_WBITS | 32)
            self._feed, self._flush = inflater.decompress, inflater.flush
        elif self.encoding == "br":
            try:
                import brotli
            except ImportError as error:
                raise OutputBlocked("brotli is not available") from error
            decompressor = brotli.Decompressor()
            self._feed, self._flush = decompressor.process, (lambda: b"")
        elif self.encoding == "zstd":
            try:
                import zstandard
            except ImportError as error:
                raise OutputBlocked("zstandard is not available") from error
            decompressor = zstandard.ZstdDecompressor().decompressobj()
            self._feed, self._flush = decompressor.decompress, decompressor.flush
        else:
            raise OutputBlocked(f"Unsupported content encoding {self.encoding!r}")

    def feed(self, chunk: bytes) -> bytes:
        try:
            return self._feed(chunk) if chunk else self._flush()
        except Exception as error:  # noqa: BLE001 - every codec has its own error type
            raise OutputBlocked("AI response could not be decompressed") from error


@final
class ResponseBody:
    """The body callback: checked bytes out, and nothing after a refusal."""

    def __init__(self, filters: ContentFilter, kind: Kind, decoder: Decoder,
                 stream: BrowserStream | LineStream | None,
                 note: Callable[[OutputBlocked], None]) -> None:
        self.filters = filters
        self.kind = kind
        self.decoder = decoder
        self.stream = stream
        self.note = note
        self.buffer = bytearray()
        self.refused = False

    def __call__(self, chunk: bytes) -> list[bytes]:
        # A list, never b"": mitmproxy writes whatever bytes come back, and
        # on HTTP/1.1 an empty chunk is the end of the body. Chrome speaks
        # HTTP/2 and never noticed; Claude Code's HTTP/1.1 client did.
        output = self.output(chunk)
        return [output] if output else []

    def output(self, chunk: bytes) -> bytes:
        if self.refused:
            return b""
        try:
            data = self.decoder.feed(chunk)
            if self.stream is not None:
                output = self.stream.feed(data)
                if not chunk:
                    output += self.stream.finish()
                return output
            self.buffer.extend(data)
            if len(self.buffer) > MAX_EVENT_BYTES:
                raise OutputBlocked("AI JSON exceeds the inspection limit")
            if chunk:
                return b""
            parsed = parse(bytes(self.buffer))
            self.buffer.clear()
            return dumps(self.filters.clean(parsed)).encode()
        except (OutputBlocked, InvalidEventStream, UnicodeError, RecursionError) as error:
            self.refused = True
            self.buffer.clear()
            blocked = error if isinstance(error, OutputBlocked) else OutputBlocked(str(error))
            log.info("AI answer refused: %s", error)
            self.note(blocked)
            return self.refusal()

    def refusal(self) -> bytes:
        if self.kind == "events":
            return b"event: error\ndata: " + ERROR + b"\n\n"
        if self.kind == "lines":
            return dumps({"version": 1, "type": "end"}).encode() + b"\n"
        return ERROR


@final
class AIFilter:
    """Reuse account policy and detector instances owned by the main filter."""

    def __init__(self, owner: FilterOwner) -> None:
        self.owner = owner
        self.sockets: dict[str, SocketFilter | str] = {}

    # ---- policy -----------------------------------------------------------

    def _filters(self, uid: int) -> ContentFilter:
        policy = self.owner.policy
        level = policy.media_level_for(uid)
        tolerance = CONTENT_TOLERANCE.get(level, content.NSFW)
        if siterules.GATING_CATEGORY in policy.blocked_categories_for(uid) \
                and content.SEVERITY[tolerance] > content.SEVERITY[content.IMMODEST]:
            tolerance = content.IMMODEST
        text = TextFilter(TextPolicy(policy.language_filter_for(uid), tolerance),
                          (self.owner.wordlist, self.owner.scorer))
        return ContentFilter(text, ImageFilter(level, self._judge))

    def _judge(self, data: bytes) -> vision.ImageVerdict | None:
        # Generated content must not inherit the ordinary web icon exemption.
        if len(data) < vision.MIN_IMAGE_BYTES:
            return None
        return self.owner.vision.verdict(data)

    def _note(self, flow: http.HTTPFlow, uid: int, error: OutputBlocked) -> None:
        """One line in the activity log; never a filtering failure."""
        try:
            activity.record("proxy", activity.BLOCK, uid, url=flow.request.pretty_url,
                            why="ai:" + error.reason)
        except Exception:  # noqa: BLE001 - the log is a convenience
            log.debug("could not record activity", exc_info=True)

    # ---- hooks ------------------------------------------------------------

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None or provider(flow.request.pretty_host) is None:
            return
        if not is_asset(flow.request.path) or provider(flow.request.pretty_host) == "api":
            # The body callback sees wire bytes; ask for them uncompressed.
            flow.request.headers["accept-encoding"] = "identity"

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        uid = flow.metadata.get("kosher_uid")
        response = flow.response
        if uid is None or response is None:
            return  # not a filtered account's connection (the main addon streams it)
        selected = provider(flow.request.pretty_host)
        if selected is None:
            return
        kind = body_kind(selected, flow)
        if kind is None:
            return
        filters = self._filters(uid)
        note = lambda error, flow=flow, uid=uid: self._note(flow, uid, error)  # noqa: E731
        try:
            decoder = Decoder(header(response.headers, "content-encoding"))
        except OutputBlocked as error:
            log.info("AI answer refused before its body: %s", error)
            note(error)
            if kind == "json":
                response.status_code = 502
            response.stream = _refused(kind)
        else:
            stream: BrowserStream | LineStream | ApiStream | None = None
            if kind == "events":
                stream = ApiStream(filters) if api_shaped(selected, flow) else BrowserStream(filters)
            elif kind == "lines":
                stream = LineStream(filters, note)
            response.stream = ResponseBody(filters, kind, decoder, stream, note)
        for name in DROPPED_HEADERS:
            if name in response.headers:
                del response.headers[name]
        response.headers["cache-control"] = "no-store"
        response.headers["x-kosheros"] = "ai-inspected"

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        uid = flow.metadata.get("kosher_uid")
        if flow.websocket is None or uid is None:
            return
        if provider(flow.request.pretty_host) not in {"chatgpt", "api"}:
            return
        message = flow.websocket.messages[-1]
        if getattr(message, "injected", False):
            return  # a frame this addon added, already checked
        state = self.sockets.get(flow.id)
        if state == REFUSED:
            # The notice went out on the last frame; nothing more passes either way.
            message.drop()
            flow.kill()
            return
        if message.from_client:
            return
        try:
            if not message.is_text:
                raise OutputBlocked("Unsupported AI websocket protocol")
            socket = state or self.sockets.setdefault(flow.id, SocketFilter(self._filters(uid)))
            frames = socket.feed(message.content)
            message.content = frames[0] if frames else b""
            for extra in frames[1:]:
                _inject(flow, extra)
        except (OutputBlocked, InvalidEventStream, UnicodeError, RecursionError) as error:
            message.content = ERROR
            self.sockets[flow.id] = REFUSED
            log.info("AI websocket refused: %s", error)
            blocked = error if isinstance(error, OutputBlocked) else OutputBlocked(str(error))
            self._note(flow, uid, blocked)

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        self.sockets.pop(flow.id, None)

    def error(self, flow: http.HTTPFlow) -> None:
        self.sockets.pop(flow.id, None)


def _inject(flow: http.HTTPFlow, frame: bytes) -> None:
    """Send one more frame to the client, after the one being handled."""
    master = getattr(ctx, "master", None)
    if master is None:  # outside a running proxy (tests)
        log.debug("no proxy master to inject a websocket frame into")
        return
    master.commands.call("inject.websocket", flow, True, frame, True)


def _refused(kind: Kind) -> Callable[[bytes], list[bytes]]:
    """A body callback that drops every upstream byte and sends the notice once."""
    sent = False

    def body(chunk: bytes) -> list[bytes]:
        nonlocal sent
        if sent:
            return []
        sent = True
        if kind == "events":
            return [b"event: error\ndata: " + ERROR + b"\n\n"]
        if kind == "lines":
            return [dumps({"version": 1, "type": "end"}).encode() + b"\n"]
        return [ERROR]

    return body
