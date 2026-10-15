"""Answers of a shape nobody taught the filter, from hosts nobody listed."""

import json

import pytest
from kosherd.ai_api import ApiLines, ApiStream
from kosherd.ai_generic import GenericText
from kosherd.ai_images import ImageFilter
from kosherd.ai_json import ContentFilter
from kosherd.ai_sse import Decoder
from kosherd.ai_text import OutputBlocked, TextFilter, TextPolicy
from kosherd.content import Scorer
from kosherd.language import Wordlist


def filters(language: str = "substitute") -> ContentFilter:
    return ContentFilter(TextFilter(TextPolicy(language, "nsfw"), (
        Wordlist({"damn": "darn"}),
        Scorer({"restricted": ("nsfw", 30), "content": ("nsfw", 30)}),
    )), ImageFilter("all", lambda _: None))


def sse(payload: str, name: str | None = None) -> bytes:
    return (f"event: {name}\n" if name else "") .encode() + b"data: " + payload.encode() + b"\n\n"


@pytest.mark.parametrize("value, expected", [
    ("I like pie", True),
    (" damn", True),
    ("damn", True),
    ("msg_06038334655e5f33016ac866e6b3e4819cb82abaac970a56a2", False),
    ("eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.abc", False),
    ("https://example.org/a/b?c=d", False),
    ("", False),
    ("   ", False),
])
def test_prose_is_anything_with_whitespace_or_a_listed_word(value: str, expected: bool) -> None:
    assert GenericText(filters()).prose(value) is expected


def test_unknown_shape_has_its_prose_checked_and_its_identifiers_kept() -> None:
    # Given a stream of some site's own invention.
    stream = ApiStream(filters(), lenient=True)
    wire = sse(json.dumps({"id": "abc_damn_123", "answer": {"chunk": "a damn fine", "ref": "https://x.test/damn"}}))
    wire += sse(json.dumps({"id": "abc_damn_124", "answer": {"chunk": " pie indeed."}}))
    # When it streams through, then prose is replaced and identifiers are untouched.
    events = [json.loads(e.data) for e in Decoder().feed(stream.feed(wire) + stream.finish())]
    assert [e["answer"]["chunk"] for e in events] == ["a darn fine", " pie indeed."]
    assert events[0]["id"] == "abc_damn_123" and events[0]["answer"]["ref"] == "https://x.test/damn"


def test_unknown_shape_is_scored_as_one_stream() -> None:
    # Given a stream whose events are each harmless but add up to a verdict.
    stream = ApiStream(filters(), lenient=True)
    first = stream.feed(sse(json.dumps({"t": "some restricted words"})))
    assert b"restricted" in first
    # When the corroborating term arrives, then the stream is refused.
    with pytest.raises(OutputBlocked):
        stream.feed(sse(json.dumps({"t": "and content too"})))


def test_plain_text_events_on_an_unlisted_host_are_prose() -> None:
    stream = ApiStream(filters(), lenient=True)
    output = stream.feed(sse("a damn fine line") + sse("ok"))
    assert output == sse("a darn fine line") + sse("ok")


def test_plain_text_events_on_a_listed_host_are_refused() -> None:
    stream = ApiStream(filters(), lenient=False)
    with pytest.raises(OutputBlocked):
        stream.feed(sse("not json"))


def test_lines_of_an_unknown_shape_are_checked() -> None:
    stream = ApiLines(filters(), lenient=True)
    wire = b'{"delta":"a damn"}\n{"delta":" idea"}\nplain damn text\n'
    output = stream.feed(wire) + stream.finish()
    assert output == b'{"delta":"a darn"}\n{"delta":" idea"}\nplain darn text\n'


def test_a_known_shape_still_gets_the_full_treatment_on_an_unlisted_host() -> None:
    # Given an OpenAI-compatible stream from some self-hosted gateway.
    stream = ApiStream(filters(), lenient=True)
    chunk = lambda text: json.dumps({"object": "chat.completion.chunk", "choices": [  # noqa: E731
        {"index": 0, "delta": {"content": text}, "finish_reason": None}]})
    wire = sse(chunk("a da")) + sse(chunk("mn split")) + sse("[DONE]")
    # When the word is split across events, then it is still caught.
    events = [e.data for e in Decoder().feed(stream.feed(wire) + stream.finish())]
    text = "".join(json.loads(e)["choices"][0]["delta"].get("content", "") for e in events if e != "[DONE]")
    assert text == "a darn split"
