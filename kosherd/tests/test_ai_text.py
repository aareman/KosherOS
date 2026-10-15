"""Generated text is inspected across provider delta boundaries."""

import pytest
from kosherd.ai_text import OutputBlocked, TextFilter, TextPolicy, TextStream
from kosherd.content import Scorer
from kosherd.language import Wordlist


def checker(mode: str = "substitute") -> TextFilter:
    return TextFilter(TextPolicy(mode, "nsfw"), (
        Wordlist({"damn": "darn"}),
        Scorer({"restricted": ("nsfw", 30), "content": ("nsfw", 30)}),
    ))


def test_substitutes_word_split_across_events() -> None:
    # Given a provider that splits a listed word across deltas.
    stream = TextStream(checker())
    # When the deltas arrive and the content block ends.
    result = stream.push("a da") + stream.push("mn shame") + stream.finish()
    # Then no original spelling reaches the consumer.
    assert result == "a darn shame"


def test_blocks_content_split_across_events() -> None:
    # Given a content term split across provider events.
    stream = TextStream(checker())
    assert stream.push("restr") == ""
    # When enough text arrives to identify it, then it is refused.
    with pytest.raises(OutputBlocked):
        stream.push("icted content")


def test_releases_checked_text_before_end_of_answer() -> None:
    # Given an answer longer than the inspection tail.
    stream = TextStream(checker())
    text = "A useful answer. " * 100
    # When the first portion arrives.
    early = stream.push(text)
    # Then content streams before EOF and reconstructs without duplication.
    assert early
    assert early + stream.finish() == text


def test_block_mode_never_substitutes() -> None:
    # Given an account whose language policy is block.
    stream = TextStream(checker("block"))
    # When a listed word arrives, then no replacement is released.
    with pytest.raises(OutputBlocked):
        stream.push("damn")


def test_output_blocks_have_independent_context() -> None:
    # Given two responses being generated concurrently.
    first, second = TextStream(checker()), TextStream(checker())
    # When their deltas interleave.
    first.push("da")
    second.push("mn")
    # Then they cannot combine into a different user's listed word.
    assert first.finish() == "da"
    assert second.finish() == "mn"
