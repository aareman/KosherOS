"""The addon on real mitmproxy flows: what it touches, what it leaves alone."""

import gzip
import json

import pytest

pytest.importorskip("mitmproxy")
from kosherd import ai_proxy
from kosherd.ai_images import BLANK_IMAGE
from kosherd.ai_lines import BLOCKED_TEXT
from kosherd.ai_proxy import AIFilter, provider
from kosherd.content import Scorer
from kosherd.language import Wordlist
from mitmproxy import http
from mitmproxy.test.tflow import tflow


class Policy:
    def __init__(self, level: str = "all", language: str = "substitute") -> None:
        self.level, self.language = level, language

    def media_level_for(self, uid):
        return self.level

    def language_filter_for(self, uid):
        return self.language

    def blocked_categories_for(self, uid):
        return []


class Vision:
    def verdict(self, data):
        return None


class Owner:
    def __init__(self, **policy) -> None:
        self.policy = Policy(**policy)
        self.wordlist = Wordlist({"damn": "darn"})
        self.scorer = Scorer({"restricted": ("nsfw", 30), "content": ("nsfw", 30)})
        self.vision = Vision()


def flow_for(url: str, status: int = 200, body: bytes = b"", uid: int | None = 1000,
             **headers: str) -> http.HTTPFlow:
    flow = tflow(resp=True)
    flow.request = http.Request.make("GET", url)
    flow.response = http.Response.make(status, body, headers)
    if uid is not None:
        flow.metadata["kosher_uid"] = uid
    return flow


def out(flow: http.HTTPFlow, chunk: bytes) -> bytes:
    """One call of the body callback: the bytes it hands mitmproxy to send."""
    stream = flow.response.stream
    assert callable(stream)
    sent = stream(chunk)
    assert isinstance(sent, list), "an empty chunk would end an HTTP/1.1 body"
    return b"".join(sent)


def drive(flow: http.HTTPFlow, *chunks: bytes) -> bytes:
    """Feed the body callback the way mitmproxy does, ending with b''."""
    return b"".join(out(flow, chunk) for chunk in (*chunks, b""))


@pytest.mark.parametrize("host, expected", [
    ("chatgpt.com", "chatgpt"),
    ("ab.chatgpt.com", "chatgpt"),
    ("chat.openai.com", "chatgpt"),
    ("claude.ai", "claude"),
    ("api.claude.ai", "claude"),
    ("api.anthropic.com", "api"),
    ("api.openai.com", "api"),
    ("generativelanguage.googleapis.com", "api"),
    ("openrouter.ai", "api"),
    ("mcp-proxy.anthropic.com", None),
    ("chatgpt.com.evil.test", None),
    ("notchatgpt.com", None),
    ("example.org", None),
])
def test_provider_is_the_site_or_one_of_its_subdomains(host: str, expected) -> None:
    assert provider(host) == expected


@pytest.mark.parametrize("url, status, content_type", [
    ("https://chatgpt.com/backend-api/sentinel/sdk.js", 200, "text/javascript; charset=utf-8"),
    ("https://chatgpt.com/backend-api/files/x/download", 302, ""),
    ("https://chatgpt.com/unauth-mweb/events/business", 204, ""),
    ("https://chatgpt.com/backend-api/conversation/x", 304, ""),
    ("https://claude.ai/api/organizations/o/chat_conversations/c", 204, ""),
    ("https://chatgpt.com/backend-api/me", 200, "application/json"),
    ("https://chatgpt.com/backend-api/estuary/content", 200, "image/png"),
    ("https://chatgpt.com/backend-api/conversation/x", 500, "application/json"),
])
def test_everything_that_is_not_an_answer_is_left_exactly_as_it_was(url, status, content_type) -> None:
    # Given a response on an AI site that carries no answer.
    addon = AIFilter(Owner())
    headers = {"content-type": content_type} if content_type else {}
    flow = flow_for(url, status, b"abcdefg", **headers)
    before = list(flow.response.headers.items(multi=True))
    # When the headers hook runs, then nothing about the response changed.
    addon.responseheaders(flow)
    assert flow.response.status_code == status
    assert flow.response.stream is False
    assert list(flow.response.headers.items(multi=True)) == before


def test_an_unfiltered_connection_is_not_inspected() -> None:
    addon = AIFilter(Owner())
    flow = flow_for("https://chatgpt.com/backend-api/f/conversation", uid=None,
                    **{"content-type": "text/event-stream"})
    addon.responseheaders(flow)
    assert flow.response.stream is False


def test_requests_ask_for_uncompressed_answers_but_not_assets() -> None:
    addon = AIFilter(Owner())
    chat = flow_for("https://chatgpt.com/unauth-mweb/conversation/updates?x=1")
    chat.response = None
    addon.request(chat)
    assert chat.request.headers["accept-encoding"] == "identity"
    asset = flow_for("https://chatgpt.com/unauth-mweb/assets/app-abc.js?v=2")
    asset.response = None
    asset.request.headers["accept-encoding"] = "br, gzip"
    addon.request(asset)
    assert asset.request.headers["accept-encoding"] == "br, gzip"


def test_conversation_lines_are_checked_as_they_stream() -> None:
    # Given ChatGPT's conversation stream, with the answer still being generated.
    addon = AIFilter(Owner())
    flow = flow_for("https://chatgpt.com/unauth-mweb/conversation/updates",
                    **{"content-type": "application/vnd.openai.conversation-source+ndjson; charset=utf-8",
                       "content-encoding": "Identity", "content-length": "999"})
    addon.responseheaders(flow)
    assert flow.response.headers["x-kosheros"] == "ai-inspected"
    assert "content-length" not in flow.response.headers
    assert "content-encoding" not in flow.response.headers
    assert flow.response.headers["cache-control"] == "no-store"
    first = json.dumps({"version": 1, "type": "content", "messageId": "m", "revision": 1,
                        "mode": "replace", "status": "streaming",
                        "markdown": "a damn long answer. " * 40}).encode() + b"\n"
    done = json.dumps({"version": 1, "type": "content", "messageId": "m", "revision": 2,
                       "mode": "append", "baseRevision": 1, "status": "complete",
                       "markdown": ""}).encode() + b"\n"
    # When the first line arrives, then checked text is already on its way.
    early = out(flow, first)
    assert b"darn long" in early and b"damn" not in early
    rest = out(flow, done) + out(flow, b"")
    assert b"damn" not in rest
    assert json.loads(rest.split(b"\n")[0])["status"] == "complete"


def test_a_compressed_answer_is_decoded_and_checked_not_refused() -> None:
    # Given a server that compressed the stream although identity was asked for.
    addon = AIFilter(Owner())
    flow = flow_for("https://chatgpt.com/backend-api/f/conversation",
                    **{"content-type": "text/event-stream", "content-encoding": "gzip"})
    addon.responseheaders(flow)
    wire = (b'data: {"message":{"status":"finished_successfully","content":'
            b'{"parts":["a damn fine answer"]}}}\n\ndata: [DONE]\n\n')
    packed = gzip.compress(wire)
    # When it arrives in pieces, then the checked plain text comes out.
    output = drive(flow, packed[:10], packed[10:])
    assert b"darn fine answer" in output
    assert b"damn" not in output
    assert b"[DONE]" in output
    assert "content-encoding" not in flow.response.headers


def test_an_unreadable_encoding_is_refused_without_forwarding_a_byte() -> None:
    # Given an encoding the addon cannot decode.
    addon = AIFilter(Owner())
    flow = flow_for("https://chatgpt.com/backend-api/f/conversation",
                    **{"content-type": "text/event-stream", "content-encoding": "x-secret"})
    addon.responseheaders(flow)
    # When the upstream body flows, then the client sees the notice and nothing else.
    output = drive(flow, b"data: unchecked damn words\n\n", b"data: [DONE]\n\n")
    assert output == b"event: error\ndata: " + ai_proxy.ERROR + b"\n\n"
    assert b"unchecked" not in output and b"damn" not in output


def test_a_malformed_stream_emits_an_error_and_drops_what_follows() -> None:
    addon = AIFilter(Owner())
    flow = flow_for("https://claude.ai/api/organizations/o/chat_conversations/c/completion",
                    **{"content-type": "text/event-stream"})
    addon.responseheaders(flow)
    output = out(flow, b"event: content_block_delta\ndata: {broken}\n\n")
    assert b"kosher_content_blocked" in output
    assert flow.response.stream(b"data: unchecked following payload\n\n") == []


def test_history_json_is_checked_before_any_byte_is_released() -> None:
    # Given a conversation being opened, with text and an inline picture.
    addon = AIFilter(Owner())
    flow = flow_for("https://claude.ai/api/organizations/o/chat_conversations/c?tree=True",
                    **{"content-type": "application/json"})
    addon.responseheaders(flow)
    source = json.dumps({"chat_messages": [{"text": "damn", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "YQ=="}},
    ]}]}).encode()
    # When it arrives in incomplete chunks, then nothing is sent (not even an empty chunk).
    assert flow.response.stream(source[:20]) == []
    assert flow.response.stream(source[20:]) == []
    output = json.loads(out(flow, b""))
    # Then the only released document carries checked text and image data.
    message = output["chat_messages"][0]
    assert message["text"] == "darn"
    assert message["content"][0]["source"]["data"] == BLANK_IMAGE
    assert message["content"][0]["source"]["media_type"] == "image/png"


def test_a_refusal_is_written_to_the_activity_log(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an account whose language filter blocks.
    noted: list[dict] = []
    monkeypatch.setattr(ai_proxy.activity, "record",
                        lambda writer, kind, uid, **fields: noted.append({"kind": kind, "uid": uid} | fields))
    addon = AIFilter(Owner(language="block"))
    flow = flow_for("https://chatgpt.com/unauth-mweb/conversation/updates",
                    **{"content-type": "application/vnd.openai.conversation-source+ndjson"})
    addon.responseheaders(flow)
    wire = json.dumps({"version": 1, "type": "content", "messageId": "m", "revision": 1,
                       "mode": "replace", "status": "complete", "markdown": "damn"}).encode() + b"\n"
    # When a listed word arrives, then the notice goes out and the log says why.
    output = drive(flow, wire)
    assert BLOCKED_TEXT.encode() in output and b"damn" not in output
    assert noted == [{"kind": "block", "uid": 1000,
                      "url": "https://chatgpt.com/unauth-mweb/conversation/updates",
                      "why": "ai:language"}]
