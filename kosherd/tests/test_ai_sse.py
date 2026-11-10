"""SSE framing must not depend on network packet boundaries."""

import pytest
from kosherd.ai_sse import Decoder, Event, InvalidEventStream


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r"])
def test_frames_unicode_at_every_byte_boundary(separator: bytes) -> None:
    # Given a Unicode event with multiple data lines and resumption fields.
    lines = (b"id: 7", b"event: delta", "data: שלום".encode(), b"data: world")
    wire = separator.join(lines) + separator * 2
    decoder = Decoder()
    # When every byte arrives separately.
    events = [event for byte in wire for event in decoder.feed(bytes([byte]))]
    decoder.finish()
    # Then framing and UTF-8 decoding are independent of the packets.
    assert events == [Event(lines)]
    assert events[0].data == "שלום\nworld"


def test_replacement_preserves_protocol_fields() -> None:
    # Given a completed event carrying an ID and a retry interval.
    event = Event((b"id: 3", b"retry: 1000", b"event: delta", b"data: original"))
    # When content is replaced.
    encoded = event.encode("safe\ntext")
    # Then the client can still dispatch and resume the event.
    assert encoded == b"id: 3\nretry: 1000\nevent: delta\ndata: safe\ndata: text\n\n"


def test_refuses_oversized_unterminated_event() -> None:
    # Given a bounded decoder.
    decoder = Decoder(limit=8)
    # When an upstream never terminates its event, then memory is bounded.
    with pytest.raises(InvalidEventStream):
        decoder.feed(b"data: " + b"x" * 20)


def test_refuses_truncated_event_at_eof() -> None:
    # Given a stream cut off before its event delimiter.
    decoder = Decoder()
    decoder.feed(b'data: {"text":"partial"}\n')
    # When EOF arrives, then the partial payload cannot be released.
    with pytest.raises(InvalidEventStream):
        decoder.finish()


def test_comment_and_empty_data_are_distinct() -> None:
    # Given a heartbeat and an empty data event in one network chunk.
    decoder = Decoder()
    # When decoded, then only the second event has a data payload.
    events = decoder.feed(b": keepalive\n\ndata\n\n")
    assert [event.data for event in events] == [None, ""]


def test_fragmented_utf8_bom_cannot_hide_first_payload() -> None:
    # Given the optional SSE UTF-8 byte order mark, fragmented on the wire.
    decoder = Decoder()
    # When decoded one byte at a time, then the first data event is inspected.
    wire = b'\xef\xbb\xbfdata: {"text":"first"}\n\n'
    events = [event for byte in wire for event in decoder.feed(bytes([byte]))]
    assert [event.data for event in events] == ['{"text":"first"}']
