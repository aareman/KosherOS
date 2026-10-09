"""Replay ChatGPT's browser event stream through the actual filtering adapters."""

import json

import pytest
from kosherd.ai_browser import BrowserStream
from kosherd.ai_images import ImageFilter
from kosherd.ai_json import ContentFilter
from kosherd.ai_socket import SocketFilter
from kosherd.ai_sse import Decoder
from kosherd.ai_text import OutputBlocked, TextFilter, TextPolicy
from kosherd.content import Scorer
from kosherd.language import Wordlist


def filters() -> ContentFilter:
    return ContentFilter(TextFilter(TextPolicy("substitute", "nsfw"), (
        Wordlist({"damn": "darn"}), Scorer(),
    )), ImageFilter("all", lambda _: None))


def event(data: dict | str, name: str = "message") -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def test_chatgpt_inherited_delta_is_checked_and_tail_is_flushed() -> None:
    # Given the browser's delta-v1 stream, including inherited path/operation.
    stream = BrowserStream(filters())
    root = {"message": {"author": {"role": "assistant"}, "status": "in_progress",
                        "content": {"content_type": "text", "parts": [""]}}}
    wire = event("v1", "delta_encoding") + event({"c": 2, "v": root}, "delta")
    wire += event({"p": "/message/content/parts/0", "o": "append", "v": "a da"}, "delta")
    wire += event({"v": "mn shame"}, "delta") + b"data: [DONE]\n\n"
    # When arbitrary fragments reach the adapter.
    output = b"".join(stream.feed(wire[i:i + 7]) for i in range(0, len(wire), 7))
    stream.finish()
    # Then no original spelling escapes and final replacement has the full text.
    assert b"damn" not in output
    events = Decoder().feed(output)
    final = json.loads(events[-2].data)
    assert final["v"]["message"]["content"]["parts"] == ["a darn shame"]
    assert final["c"] == 2


def test_chatgpt_streams_checked_portion_before_done() -> None:
    # Given a long browser snapshot that is still being generated.
    stream = BrowserStream(filters())
    wire = event({"message": {"status": "in_progress", "content": {
        "parts": ["Useful words. " * 100], "content_type": "text",
    }}})
    # When received, then a checked prefix is visible before completion.
    output = stream.feed(wire)
    parts = json.loads(Decoder().feed(output)[0].data)["message"]["content"]["parts"]
    assert 0 < len(parts[0]) < len("Useful words. " * 100)


def test_websocket_topics_do_not_share_delta_headers() -> None:
    # Given two independent generations on one ChatGPT websocket.
    socket = SocketFilter(filters())
    def envelope(topic: str, text: str) -> dict:
        encoded = event({"c": 0, "v": {"message": {"status": "finished_successfully",
                         "content": {"parts": [text]}}}}, "delta").decode()
        return {"topic_id": topic, "payload": {"payload": {"encoded_item": encoded}}}
    # When the envelope carries both topics.
    output = socket.feed(json.dumps([envelope("one", "damn"), envelope("two", "hello")]).encode())[0]
    # Then each response is independently checked with its identity preserved.
    assert b"damn" not in output
    assert b"darn" in output and b"hello" in output
    assert len(socket.topics) == 2


def test_unknown_chatgpt_encoding_is_refused() -> None:
    # Given a provider protocol version we cannot inspect.
    stream = BrowserStream(filters())
    # When announced, then it fails closed.
    with pytest.raises(OutputBlocked):
        stream.feed(event("v99", "delta_encoding"))


def test_legacy_chatgpt_flushes_short_answer_at_done() -> None:
    # Given a short legacy snapshot without a separate final-status update.
    stream = BrowserStream(filters())
    wire = event({"message": {"content": {"parts": ["a damn shame"]}}})
    # When DONE arrives, then the withheld tail is present in the final snapshot.
    values = Decoder().feed(stream.feed(wire + b"data: [DONE]\n\n"))
    assert json.loads(values[-2].data)["message"]["content"]["parts"] == ["a darn shame"]
