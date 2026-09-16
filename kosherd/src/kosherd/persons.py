"""A person detector, for the pictures the nudity model sees nothing in.

The nudity model labels parts: a face, a belly, a covered chest. A skirt
photographed from the hips down has none of those — legs are not a class
— so it came back empty, the skin measurement had nothing to hang on, and
"miniskirts seem to get through a lot". Measured on Amazon's own grid for
that search, eleven of sixty-two pictures got through that way, every one
a hips-down crop.

A skin rule with no figure to anchor it is not an option: tried on the
same grid against a hundred and fifty beige products, any threshold that
caught most of the legs hid three quarters of the furniture too. What is
missing is the answer to "is there a person here at all", and that is a
solved problem with a small model. YOLOX-Nano (Megvii, Apache-2.0) is
3.5 MB and answers in about forty milliseconds on a laptop CPU; it found
the person in ten of those eleven crops, and the "people" it found among
the furniture were men modelling trousers.

Only the person class is read, only when the nudity model found no face,
and only when the picture has enough skin-toned area to be worth asking.
The box it returns is what the skin measurement in vision.py then runs on.

Preprocessing and decoding are pure functions on arrays, tested without
the model; the model itself is loaded lazily and its absence — no file, no
onnxruntime — means the detector says "cannot tell" rather than "nobody".
"""

from __future__ import annotations

import io
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("kosherd.persons")

MODEL_PATH = Path(os.environ.get("KOSHER_PERSON_MODEL",
                                 "/usr/share/kosher/models/yolox_nano.onnx"))
MODEL_URL = ("https://github.com/Megvii-BaseDetection/YOLOX/releases/download/"
             "0.1.1rc0/yolox_nano.onnx")
MODEL_SHA256 = "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"

INPUT = 416          # the side of the square the model was exported for
PAD = 114            # YOLOX's letterbox fill
STRIDES = (8, 16, 32)
PERSON_CLASS = 0     # COCO
PERSON_CONFIDENCE = 0.5
IOU_LIMIT = 0.5
# Below this much skin-toned area in the whole picture there is nothing the
# skin measurement could find, so the model is not asked.
WORTH_ASKING = 0.08


# -- the arithmetic, testable without the model ---------------------------------

def letterbox(image, size: int = INPUT):
    """A PIL image -> (float32 array [1, 3, size, size] in BGR, the scale
    the image was resized by). Top-left aligned, grey-padded, no
    normalisation: what the exported model expects."""
    import numpy as np

    width, height = image.size
    ratio = min(size / width, size / height)
    new_w, new_h = max(1, int(width * ratio)), max(1, int(height * ratio))
    canvas = np.full((size, size, 3), PAD, dtype=np.uint8)
    resized = np.asarray(image.convert("RGB").resize((new_w, new_h)), dtype=np.uint8)
    canvas[:new_h, :new_w] = resized[:, :, ::-1]  # RGB -> BGR
    return canvas.astype(np.float32).transpose(2, 0, 1)[None], ratio


def grids(size: int = INPUT):
    """The anchor-point grid and the stride of each row of the model's
    output, in the order the model emits them."""
    import numpy as np

    points, strides = [], []
    for stride in STRIDES:
        n = size // stride
        ys, xs = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        points.append(np.stack((xs, ys), 2).reshape(-1, 2))
        strides.append(np.full((n * n, 1), stride))
    return np.concatenate(points).astype(np.float32), np.concatenate(strides).astype(np.float32)


def decode(raw, size: int = INPUT):
    """Raw model output [N, 5 + classes] -> the same with the first four
    columns as centre x, centre y, width, height in input pixels."""
    import numpy as np

    points, strides = grids(size)
    out = np.array(raw, dtype=np.float32, copy=True)
    out[:, :2] = (out[:, :2] + points) * strides
    out[:, 2:4] = np.exp(out[:, 2:4]) * strides
    return out


def people(decoded, ratio: float, width: int, height: int,
           threshold: float = PERSON_CONFIDENCE) -> list[tuple[float, tuple[int, int, int, int]]]:
    """(score, (x, y, w, h)) in the original picture's pixels, best first,
    overlapping boxes merged away, for every person above `threshold`."""
    scores = decoded[:, 4] * decoded[:, 5 + PERSON_CLASS]
    found = []
    for (cx, cy, bw, bh), score in zip(decoded[scores >= threshold, :4],
                                        scores[scores >= threshold]):
        x0 = max(0.0, (cx - bw / 2) / ratio)
        y0 = max(0.0, (cy - bh / 2) / ratio)
        x1 = min(float(width), (cx + bw / 2) / ratio)
        y1 = min(float(height), (cy + bh / 2) / ratio)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        found.append((float(score), (int(x0), int(y0), int(x1 - x0), int(y1 - y0))))
    found.sort(reverse=True)
    kept: list[tuple[float, tuple[int, int, int, int]]] = []
    for candidate in found:
        if all(iou(candidate[1], other[1]) < IOU_LIMIT for other in kept):
            kept.append(candidate)
    return kept


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


# -- the model -------------------------------------------------------------------

class PersonDetector:
    def __init__(self, model_path: Path = MODEL_PATH):
        self.model_path = Path(model_path)
        self._session = None
        self._input = None
        self._failed = False
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._session is not None or self._failed:
            return self._session
        with self._lock:
            if self._session is not None or self._failed:
                return self._session
            try:
                import numpy  # noqa: F401 - the arithmetic needs it
                import onnxruntime as ort

                if not self.model_path.exists():
                    raise FileNotFoundError(self.model_path)
                options = ort.SessionOptions()
                # One picture at a time, beside the browser: keep the model
                # to a thread or two rather than every core.
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                self._session = ort.InferenceSession(
                    str(self.model_path), sess_options=options,
                    providers=["CPUExecutionProvider"])
                self._input = self._session.get_inputs()[0].name
            except Exception as e:  # noqa: BLE001 - "cannot tell", never a crash
                log.warning("no person model (%s): pictures with no face are judged "
                            "by the nudity model alone", e)
                self._failed = True
        return self._session

    def detect(self, image_bytes: bytes,
               threshold: float = PERSON_CONFIDENCE) -> list | None:
        """People in the picture, best first, or None when the model cannot
        be asked (missing, broken, unreadable picture)."""
        session = self._load()
        if session is None:
            return None
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as im:
                width, height = im.size
                tensor, ratio = letterbox(im)
            raw = session.run(None, {self._input: tensor})[0][0]
        except Exception:  # noqa: BLE001
            log.debug("person detection failed", exc_info=True)
            return None
        return people(decode(raw), ratio, width, height, threshold)


_default: PersonDetector | None = None


def default() -> PersonDetector:
    global _default
    if _default is None:
        _default = PersonDetector()
    return _default
