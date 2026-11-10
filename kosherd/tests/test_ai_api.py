"""API answers, in the four shapes tools speak, through the one event handler.

The Anthropic and Responses fixtures mirror what Claude Code and Codex were
seen sending and receiving through the proxy in October 2026; the Chat
Completions and Gemini ones follow the published formats.
"""

import json

import pytest
from kosherd.ai_api import ApiEvents, ApiStream
from kosherd.ai_images import ImageFilter
from kosherd.ai_json import ContentFilter
from kosherd.ai_socket import SocketFilter
from kosherd.ai_sse import Decoder
from kosherd.ai_text import OutputBlocked, TextFilter, TextPolicy
from kosherd.content import Scorer
from kosherd.language import Wordlist


def filters(language: str = "substitute") -> ContentFilter:
    return ContentFilter(TextFilter(TextPolicy(language, "nsfw"), (
        Wordlist({"damn": "darn"}),
        Scorer({"restricted": ("nsfw", 30), "content": ("nsfw", 30)}),
    )), ImageFilter("all", lambda _: None))


def sse(data: dict | str, name: str | None = None) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(data)
    name = name or (data.get("type") if isinstance(data, dict) else None)
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {payload}\n\n".encode()


def run(stream: ApiStream, wire: bytes, step: int = 5) -> list[dict | str]:
    output = b"".join(stream.feed(wire[i:i + step]) for i in range(0, len(wire), step))
    output += stream.finish()
    return [json.loads(e.data) if e.data != "[DONE]" else "[DONE]"
            for e in Decoder().feed(output) if e.data is not None]


def texts(events: list, pick) -> str:
    return "".join(pick(e) for e in events if isinstance(e, dict) and pick(e) is not None)


# ---- Anthropic Messages (Claude Code, claude.ai) ----------------------------

def anthropic_wire(pieces: list[str]) -> bytes:
    wire = sse({"type": "message_start", "message": {"id": "msg_1", "role": "assistant", "content": []}})
    wire += sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    wire += sse({"type": "ping"})
    for piece in pieces:
        wire += sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}})
    wire += sse({"type": "content_block_stop", "index": 0})
    wire += sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}})
    wire += sse({"type": "message_stop"})
    return wire


def test_claude_code_answer_is_checked_across_deltas_and_flushed_before_the_block_ends() -> None:
    # Given the stream Claude Code receives, with a listed word split across deltas.
    stream = ApiStream(filters())
    events = run(stream, anthropic_wire(["Well da", "mn, pineap", "ple pie is great."]))
    # Then every delta precedes its block stop, the word is replaced, and nothing leaks.
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert "".join(d["delta"]["text"] for d in deltas) == "Well darn, pineapple pie is great."
    kinds = [e["type"] for e in events]
    assert kinds.index("content_block_stop") > max(i for i, k in enumerate(kinds) if k == "content_block_delta")
    assert "damn" not in json.dumps(events)
    assert kinds[-1] == "message_stop" and "ping" in kinds


def test_claude_tool_arguments_are_held_and_checked_as_complete_json() -> None:
    # Given a tool input whose JSON and listed word are fragmented.
    stream = ApiStream(filters())
    wire = sse({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t", "name": "Write", "input": {}}})
    wire += sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"content":"da'}})
    wire += sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'mn"}'}})
    held = [json.loads(e.data) for e in Decoder().feed(stream.feed(wire))]
    assert all(e["delta"]["partial_json"] == "" for e in held if e["type"] == "content_block_delta")
    # When the block ends, then unsafe source is refused before any source escapes.
    with pytest.raises(OutputBlocked):
        stream.feed(sse({"type": "content_block_stop", "index": 1}))


def test_claude_clean_tool_arguments_arrive_whole_when_the_block_ends() -> None:
    stream = ApiStream(filters())
    wire = sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":"a.py","con'}})
    wire += sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'tent":"print(1)"}'}})
    wire += sse({"type": "content_block_stop", "index": 1})
    events = [json.loads(e.data) for e in Decoder().feed(stream.feed(wire))]
    assert events[-2]["delta"]["partial_json"] == '{"path":"a.py","content":"print(1)"}'
    assert events[-1]["type"] == "content_block_stop"


# ---- OpenAI Responses (Codex over a WebSocket, api.openai.com) -------------

def responses_frames(words: list[str], item: str = "msg_1") -> list[dict]:
    base = {"item_id": item, "output_index": 0, "content_index": 0}
    frames = [{"type": "codex.rate_limits", "plan_type": "x"},
              {"type": "response.created", "sequence_number": 0,
               "response": {"id": "resp_1", "status": "in_progress", "output": [],
                            "instructions": "you are a damn fine assistant"}},
              {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
               "item": {"type": "message", "id": item, "role": "assistant", "content": []}}]
    frames += [base | {"type": "response.output_text.delta", "sequence_number": 2 + i, "delta": w}
               for i, w in enumerate(words)]
    text = "".join(words)
    frames += [base | {"type": "response.output_text.done", "sequence_number": 99, "text": text},
               {"type": "response.output_item.done", "output_index": 0,
                "item": {"type": "message", "id": item, "content": [{"type": "output_text", "text": text}]}},
               {"type": "response.completed", "response": {"id": "resp_1", "status": "completed",
                "instructions": "you are a damn fine assistant",
                "output": [{"type": "message", "id": item, "content": [{"type": "output_text", "text": text}]}]}}]
    return frames


def test_codex_frames_are_checked_with_the_remainder_sent_before_done() -> None:
    # Given Codex's socket frames for a short answer.
    socket = SocketFilter(filters())
    out: list[dict] = []
    for frame in responses_frames(["Well", " damn", ",", " pineapple", " pie", " is", " great", "."]):
        out += [json.loads(f) for f in socket.feed(json.dumps(frame).encode())]
    # Then the deltas plus the remainder spell the checked answer, and every full copy agrees.
    deltas = [e for e in out if e["type"] == "response.output_text.delta"]
    assert "".join(d["delta"] for d in deltas) == "Well darn, pineapple pie is great."
    done = next(e for e in out if e["type"] == "response.output_text.done")
    assert done["text"] == "Well darn, pineapple pie is great."
    assert out.index(done) > out.index(deltas[-1])
    completed = next(e for e in out if e["type"] == "response.completed")
    assert completed["response"]["output"][0]["content"][0]["text"] == "Well darn, pineapple pie is great."
    # The echoed instructions are the person's own request, left alone.
    assert completed["response"]["instructions"] == "you are a damn fine assistant"
    assert "damn" not in json.dumps([e for e in out if e["type"] != "response.created"
                                     and e["type"] != "response.completed"])


def test_codex_function_call_arguments_are_held_then_sent_whole() -> None:
    api = ApiEvents(filters())
    base = {"item_id": "fc_1", "output_index": 1}
    first = api.event(base | {"type": "response.function_call_arguments.delta", "delta": '{"cmd":"ec'})
    second = api.event(base | {"type": "response.function_call_arguments.delta", "delta": 'ho hi"}'})
    assert first[0]["delta"] == "" and second[0]["delta"] == ""
    done = api.event(base | {"type": "response.function_call_arguments.done", "arguments": '{"cmd":"echo hi"}'})
    assert done[0]["type"] == "response.function_call_arguments.delta" and done[0]["delta"] == '{"cmd":"echo hi"}'
    assert done[1]["type"] == "response.function_call_arguments.done"


def test_codex_function_call_with_a_listed_word_is_refused_not_rewritten() -> None:
    api = ApiEvents(filters())
    with pytest.raises(OutputBlocked):
        api.event({"type": "response.function_call_arguments.done", "item_id": "fc", "output_index": 1,
                   "arguments": '{"cmd":"echo damn"}'})
    with pytest.raises(OutputBlocked):
        api.event({"type": "response.output_item.done", "output_index": 1,
                   "item": {"type": "function_call", "name": "shell", "arguments": '{"cmd":"echo damn"}'}})


def test_responses_over_sse_keeps_event_names() -> None:
    stream = ApiStream(filters())
    wire = b"".join(sse(f) for f in responses_frames(["a damn", " fine day. " * 30]))
    output = stream.feed(wire) + stream.finish()
    events = Decoder().feed(output)
    assert all(e.name == json.loads(e.data)["type"] for e in events)
    assert b"damn" not in output.replace(b"you are a damn fine assistant", b"")


# ---- OpenAI Chat Completions (most other APIs) -------------------------------

def chunk(index: int, content: str | None = None, finish: str | None = None, **delta) -> dict:
    d = dict(delta)
    if content is not None:
        d["content"] = content
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [
        {"index": index, "delta": d, "finish_reason": finish}]}


def test_chat_completion_chunks_are_checked_and_flushed_at_done() -> None:
    stream = ApiStream(filters())
    wire = sse(chunk(0, role="assistant")) + sse(chunk(0, "Well da")) + sse(chunk(0, "mn, pie"))
    wire += sse(chunk(0, finish="stop")) + sse("[DONE]")
    events = run(stream, wire)
    text = texts(events, lambda e: e["choices"][0]["delta"].get("content") if e.get("choices") else None)
    assert text == "Well darn, pie"
    assert events[-1] == "[DONE]"
    assert "damn" not in json.dumps(events)


def test_chat_completion_tool_calls_are_held_until_the_choice_finishes() -> None:
    stream = ApiStream(filters())
    call = lambda args: chunk(0, tool_calls=[{"index": 0, "id": "c", "function": {"name": "run", "arguments": args}}])  # noqa: E731
    wire = sse(call('{"cmd":')) + sse(call('"ls"}')) + sse(chunk(0, finish="tool_calls")) + sse("[DONE]")
    events = run(stream, wire)
    args = [e["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
            for e in events if isinstance(e, dict) and e["choices"][0]["delta"].get("tool_calls")]
    assert args == ["", "", '{"cmd":"ls"}']


def test_chat_completion_json_answer_is_checked_whole() -> None:
    api = ApiEvents(filters())
    out = api.event({"id": "x", "object": "chat.completion", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "damn fine"}, "finish_reason": "stop"}]})
    assert out[-1]["choices"][0]["message"]["content"] == "darn fine"


# ---- Gemini -----------------------------------------------------------------

def gemini(text: str, finish: str | None = None) -> dict:
    candidate = {"index": 0, "content": {"role": "model", "parts": [{"text": text}]}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate], "usageMetadata": {}}


def test_gemini_chunks_are_checked_across_parts() -> None:
    stream = ApiStream(filters())
    wire = sse(gemini("Well da")) + sse(gemini("mn, pie")) + sse(gemini("", "STOP"))
    events = run(stream, wire)
    text = "".join(p["text"] for e in events for p in e["candidates"][0]["content"]["parts"])
    assert text == "Well darn, pie"
    assert "damn" not in json.dumps(events)


# ---- refusals -----------------------------------------------------------------

def test_an_answer_over_the_content_level_is_refused_mid_stream() -> None:
    stream = ApiStream(filters())
    wire = anthropic_wire(["Plain start. " * 10, "restricted content", " and more. " * 20])
    with pytest.raises(OutputBlocked):
        run(stream, wire)


def test_a_truncated_api_stream_still_checks_what_it_held_back() -> None:
    # Given a stream cut off before its stop events.
    stream = ApiStream(filters())
    wire = sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "a damn short one"}})
    # When it ends, then the withheld text is released checked, not lost or leaked.
    events = run(stream, wire)
    assert [e["delta"]["text"] for e in events] == ["", "a darn short one"]
