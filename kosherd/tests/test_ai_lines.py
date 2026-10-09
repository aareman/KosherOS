"""ChatGPT's line-by-line conversation stream, as captured from chatgpt.com.

The fixture mirrors the October 2026 wire shape: a replace line with the
renderer snapshot, append lines that each carry a markdown delta plus a full
snapshot, a bare completion line, and a shared HTML template at the end that
repeats the whole answer.
"""

import json

import pytest
from kosherd.ai_images import ImageFilter
from kosherd.ai_json import ContentFilter
from kosherd.ai_lines import BLOCKED_TEXT, LineDecoder, LineStream
from kosherd.ai_text import OutputBlocked, TextFilter, TextPolicy
from kosherd.content import Scorer
from kosherd.language import Wordlist

MESSAGE = "3923ea79-39fd-40e7-bb31-4a53d6e2c903"


def filters(language: str = "substitute") -> ContentFilter:
    return ContentFilter(TextFilter(TextPolicy(language, "nsfw"), (
        Wordlist({"pineapple": "mango"}),
        Scorer({"restricted": ("nsfw", 30), "content": ("nsfw", 30)}),
    )), ImageFilter("all", lambda _: None))


def line(value: dict) -> bytes:
    return json.dumps({"version": 1} | value).encode() + b"\n"


def snapshot(paragraphs: list[str]) -> dict:
    constants = {str(i): text for i, text in enumerate(paragraphs)}
    return {"kind": "dil", "renderer": "dil-v2", "status": "pending",
            "code": "DIL.render(__dil.jsx(() => __dilConstants[\"0\"]));",
            "constants": constants, "fallbackMarkdown": "\n\n".join(paragraphs),
            "appData": {}, "viewState": {}}


def content(revision: int, mode: str, markdown: str, *, status: str = "streaming",
            paragraphs: list[str] | None = None) -> bytes:
    value = {"type": "content", "messageId": MESSAGE, "revision": revision,
             "mode": mode, "markdown": markdown, "status": status,
             "streamingParent": {"metadata": {"streaming_parent_id": "p", "streaming_parent_revision": 0}}}
    if mode == "append":
        value["baseRevision"] = revision - 1
    if paragraphs is not None:
        value["dil"] = snapshot(paragraphs)
    return line(value)


def answer(text: str, *, chunk: int = 40) -> bytes:
    """The wire for one answer, streamed the way chatgpt.com streams it."""
    wire = line({"type": "control", "detail": {"kind": "started"}})
    pieces = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    so_far = ""
    for index, piece in enumerate(pieces):
        so_far += piece
        wire += content(index + 1, "replace" if index == 0 else "append", piece,
                        paragraphs=so_far.split("\n\n"))
    wire += content(len(pieces) + 1, "append", "", status="complete")
    wire += line({"type": "state", "conversationState": json.dumps({"messages": [], "last": text})})
    wire += line({"type": "shared", "html": "<template><p>" + text + "</p></template>"})
    wire += line({"type": "end"})
    return wire


def decoded(output: bytes) -> list[dict]:
    return [json.loads(item) for item in output.split(b"\n") if item.strip()]


def run(stream: LineStream, wire: bytes, step: int = 7) -> list[dict]:
    output = b"".join(stream.feed(wire[i:i + step]) for i in range(0, len(wire), step))
    return decoded(output + stream.finish())


def test_listed_word_is_replaced_in_every_place_the_answer_appears() -> None:
    # Given an answer that names the word in its text, its snapshot and the final template.
    text = "Dov grew pineapple on a hill.\n\nEvery pineapple was sweet. " + "More words here. " * 30
    stream = LineStream(filters())
    # When it streams through in arbitrary fragments.
    output = run(stream, answer(text))
    raw = json.dumps(output)
    # Then the original word is gone everywhere and the replacement is present.
    assert "pineapple" not in raw
    assert "mango" in raw
    markdown = "".join(item["markdown"] for item in output if item.get("type") == "content")
    assert markdown == text.replace("pineapple", "mango")
    assert "mango" in [item for item in output if item.get("type") == "shared"][0]["html"]
    assert "mango" in [item for item in output if item.get("type") == "state"][0]["conversationState"]


def test_tail_is_held_back_while_streaming_and_released_on_completion() -> None:
    # Given a long answer.
    text = "A pleasant sentence about farming. " * 40
    stream = LineStream(filters())
    # When it streams.
    output = run(stream, answer(text, chunk=600))
    lines = [item for item in output if item.get("type") == "content"]
    # Then the first lines show less than has arrived, and completion shows all of it.
    assert 0 < len(lines[0]["markdown"]) < 600
    assert "".join(item["markdown"] for item in lines) == text
    # The snapshot holds its tail back on streaming lines and is complete at the end.
    first = lines[0]["dil"]["constants"]["0"]
    assert first == lines[0]["markdown"]
    assert lines[-1]["status"] == "complete"
    assert lines[-1]["dil"]["constants"]["0"] == text
    assert lines[-1]["dil"]["fallbackMarkdown"] == text


def test_refused_answer_becomes_a_notice_and_nothing_more() -> None:
    # Given an answer that earns a content verdict only once enough has arrived.
    text = "Plain start. " * 30 + "restricted content " * 5 + "and a long tail. " * 30
    stream = LineStream(filters())
    # When it streams.
    output = run(stream, answer(text))
    raw = json.dumps(output)
    # Then the word that convicted it never reaches the page.
    assert "restricted" not in raw
    lines = [item for item in output if item.get("type") == "content"]
    notice = next(item for item in lines if item["markdown"] == BLOCKED_TEXT)
    # The line keeps the shape the page expects: its snapshot shows the notice and nothing else.
    assert notice["mode"] == "append" and notice["revision"] > 1
    constants = list(notice["dil"]["constants"].values())
    assert constants[0] == BLOCKED_TEXT and all(value == "" for value in constants[1:])
    assert notice["dil"]["fallbackMarkdown"] == BLOCKED_TEXT
    assert notice["dil"]["code"].startswith("DIL.render")
    after = lines[lines.index(notice) + 1:]
    assert all(item["markdown"] == "" and item["mode"] == "append" for item in after)
    assert all(list(item["dil"]["constants"].values())[0] == BLOCKED_TEXT
               for item in after if "dil" in item)
    assert after[-1]["status"] == "complete"
    # The template that repeats the answer is kept for the page, emptied of it.
    shared = [item for item in output if item.get("type") == "shared"][0]
    assert shared["html"] == ""
    assert [item["type"] for item in output][-1] == "end"


def test_language_block_refuses_at_the_first_listed_word() -> None:
    # Given an account whose language filter blocks rather than substitutes.
    stream = LineStream(filters("block"))
    noted: list[str] = []
    stream.on_block = lambda error: noted.append(error.reason)
    # When a listed word arrives, then the page gets the notice and the log the reason.
    output = run(stream, answer("I like pineapple on pizza. " * 20))
    assert "pineapple" not in json.dumps(output)
    assert BLOCKED_TEXT in json.dumps(output)
    assert "language" in noted


def test_lines_the_filter_does_not_understand_pass_with_their_text_checked() -> None:
    # Given control lines and an unknown line type carrying prose.
    stream = LineStream(filters())
    wire = line({"type": "control", "detail": {"kind": "resume-token", "resumeToken": "abc"}})
    wire += line({"type": "future", "text": "a pineapple"})
    # When they pass through, then identifiers stay and prose is checked.
    output = run(stream, wire)
    assert output[0]["detail"]["resumeToken"] == "abc"
    assert output[1]["text"] == "a mango"


def test_unparseable_line_is_refused() -> None:
    stream = LineStream(filters())
    with pytest.raises(OutputBlocked):
        stream.feed(b'{"type": "content", "markdown": \n')


@pytest.mark.parametrize("step", [1, 3, 1000])
def test_decoder_frames_lines_whatever_the_chunking(step: int) -> None:
    wire = b'{"a":1}\n\n{"b":2}\n{"c":3}'
    decoder = LineDecoder()
    lines = [item for i in range(0, len(wire), step) for item in decoder.feed(wire[i:i + step])]
    lines += decoder.finish()
    assert lines == [b'{"a":1}', b"", b'{"b":2}', b'{"c":3}']
