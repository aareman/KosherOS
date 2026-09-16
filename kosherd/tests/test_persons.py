"""The person detector's arithmetic, without the model.

Letterboxing, grid decoding and the merge of overlapping boxes are what
turn the model's numbers into a box in the picture's own pixels; a slip in
any of them puts the skin measurement on the wrong part of the picture.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
from PIL import Image  # noqa: E402

from kosherd import persons  # noqa: E402


def test_letterbox_keeps_the_aspect_and_pads_with_grey():
    tensor, ratio = persons.letterbox(Image.new("RGB", (200, 100), (255, 0, 0)), 416)
    assert tensor.shape == (1, 3, 416, 416) and tensor.dtype == np.float32
    assert ratio == pytest.approx(416 / 200)
    # BGR: the red picture lands in channel 2; the padding is grey in all.
    assert tensor[0, 2, 0, 0] == 255 and tensor[0, 0, 0, 0] == 0
    assert tensor[0, 0, 300, 300] == persons.PAD


def test_the_grid_has_one_row_per_output_and_the_right_strides():
    points, strides = persons.grids(416)
    assert points.shape[0] == strides.shape[0] == 52 * 52 + 26 * 26 + 13 * 13
    assert strides[0, 0] == 8 and strides[-1, 0] == 32
    assert tuple(points[1]) == (1, 0), "x runs fastest"


def test_decode_places_a_box_where_the_grid_cell_is():
    raw = np.zeros((52 * 52 + 26 * 26 + 13 * 13, 85), dtype=np.float32)
    # Cell (3, 5) on the stride-8 map, centred, with log-size 0 -> 8x8.
    row = 5 * 52 + 3
    raw[row, :4] = (0.5, 0.5, 0.0, 0.0)
    out = persons.decode(raw, 416)
    assert tuple(out[row, :4]) == pytest.approx((3.5 * 8, 5.5 * 8, 8.0, 8.0))


def test_people_scales_back_clips_and_merges_overlaps():
    decoded = np.zeros((3, 85), dtype=np.float32)
    decoded[0, :4] = (100, 100, 60, 120); decoded[0, 4] = 0.9; decoded[0, 5] = 0.9
    decoded[1, :4] = (104, 102, 60, 120); decoded[1, 4] = 0.8; decoded[1, 5] = 0.9   # same person
    decoded[2, :4] = (300, 100, 40, 40); decoded[2, 4] = 0.9; decoded[2, 5] = 0.1    # not a person
    found = persons.people(decoded, ratio=2.0, width=150, height=120)
    assert len(found) == 1
    score, (x, y, w, h) = found[0]
    assert score == pytest.approx(0.81)
    assert (x, y) == (35, 20) and w == 30 and h == 60           # /ratio, clipped to the picture


def test_a_missing_model_is_unavailable_not_an_error(tmp_path):
    detector = persons.PersonDetector(tmp_path / "nowhere.onnx")
    assert detector.available() is False
    assert detector.detect(b"not a picture") is None
