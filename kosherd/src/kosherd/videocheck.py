"""Looking inside a video, a few frames at a time.

Video used to be judged only by where it came from, and an account whose
pictures are filtered got no open-web video at all: a clip is pictures at
thirty a second, and decoding all of them is out of the question on a
two-core machine. But it never needed all of them. A handful of keyframes,
spread through the clip and decoded small, tell the same detector the same
things it learns from a photograph — and a verdict per clip is cached, so
the cost is paid once per video rather than once per view.

What this does not do, and says so:

- **Large files are not held.** A clip up to VIDEO_MAX_BYTES is buffered,
  sampled and released or refused; anything bigger cannot be sampled
  without holding it, so at the modesty levels it is refused as before and
  at the mildest level it passes as before. Long-form video on the web is
  almost always segmented (HLS/DASH), and each segment is small and is
  judged on its own.
- **Frames are sampled, not watched.** Four frames from a two-minute clip
  can miss a second of anything. This is a filter for what a video is
  about, not a guarantee about every frame; the guarantee is still "all".
- **When it cannot look, it refuses.** No decoder installed, a container it
  cannot read, a deadline missed: the clip is refused for an account that
  asked for video to be checked, the same rule pictures follow.

Decoding is PyAV (FFmpeg's libraries as a wheel, with H.264, VP9 and AV1
decoders — Fedora's own ffmpeg-free cannot decode H.264). It is imported
lazily: a machine without it degrades to the source-based behaviour.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time

from .vision import (
    CLEAN,
    JUDGEMENT_VERSION,
    ImageFilter,
    ImageVerdict,
    VerdictCache,
    combine,
)

log = logging.getLogger(__name__)

# Keyframes to judge per clip: spread through the clip, not the first few
# seconds, because the first few seconds are the title card.
SAMPLE_FRAMES = 4
# Decode target for a frame's short side. The detector upscales anything
# under 480 before looking, so this is the smallest size that costs it
# nothing in accuracy.
FRAME_SHORT_SIDE = 480
# A whole clip's judgement — decode plus four detections — must finish in
# this long or the clip is refused. Generous next to a picture's 2 s: a
# person waiting for a video to start is already waiting.
VIDEO_TIMEOUT = 8.0
# The most of a clip the proxy will hold in memory to sample it. Short
# clips on news and social sites are well under this; long films arrive as
# segments, each judged on its own.
VIDEO_MAX_BYTES = 40 * 1024 * 1024
# A verdict per clip lives this long; the same address is the same clip.
VIDEO_CACHE_TTL = 30 * 24 * 3600


def available() -> bool:
    try:
        import av  # noqa: F401
    except Exception:  # noqa: BLE001 - absence is a supported state
        return False
    return True


def key(url: str, total_bytes: int | None) -> str:
    """The cache key for a clip: its address without any fragment, its
    total size when known (a re-encoded clip at the same address is a new
    clip), and the judgement version so a fix reaches cached verdicts."""
    base = url.split("#", 1)[0]
    raw = f"{base}|{total_bytes if total_bytes is not None else '?'}|v{JUDGEMENT_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()


def sample_frames(data: bytes, max_frames: int = SAMPLE_FRAMES,
                  short_side: int = FRAME_SHORT_SIDE) -> list[bytes]:
    """Up to `max_frames` keyframes from the clip, spread through it, as
    small JPEGs. Empty when the container cannot be read at all."""
    import av
    from PIL import Image

    frames: list[bytes] = []
    seen_pts: set[int] = set()

    def keep(frame) -> bool:
        pts = frame.pts if frame.pts is not None else len(frames)
        if pts in seen_pts:
            return False
        seen_pts.add(pts)
        image = frame.to_image()
        w, h = image.size
        if min(w, h) > short_side:
            scale = short_side / min(w, h)
            image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                 Image.Resampling.BILINEAR)
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=85)
        frames.append(out.getvalue())
        return True

    with av.open(io.BytesIO(data)) as container:
        streams = [s for s in container.streams if s.type == "video"]
        if not streams:
            return []
        stream = streams[0]
        stream.thread_type = "AUTO"
        # Keyframes only: they decode on their own and are all a sample
        # needs. Everything between them is skipped by the decoder itself.
        stream.codec_context.skip_frame = "NONKEY"

        duration = stream.duration
        if duration and duration > 0 and stream.time_base:
            # Seek to evenly spaced points and take the keyframe at each.
            start = stream.start_time or 0
            for i in range(max_frames):
                target = int(start + duration * (i + 0.5) / max_frames)
                try:
                    container.seek(target, stream=stream, backward=True, any_frame=False)
                    for frame in container.decode(stream):
                        keep(frame)
                        break
                except Exception:  # noqa: BLE001 - one bad seek is not a verdict
                    log.debug("seek failed", exc_info=True)
                    break
        if not frames:
            # No index to seek by (a transport-stream segment, a fragment):
            # walk the keyframes from the start and keep a spread of them.
            candidates = []
            try:
                for frame in container.decode(stream):
                    candidates.append(frame)
                    if len(candidates) >= max_frames * 4:
                        break
            except Exception:  # noqa: BLE001 - keep what was decoded
                log.debug("decode stopped early", exc_info=True)
            if candidates:
                step = max(1, len(candidates) // max_frames)
                for frame in candidates[::step][:max_frames]:
                    keep(frame)
    return frames


class VideoChecker:
    """Frames through the picture filter, with a per-clip cache."""

    def __init__(self, images: ImageFilter, cache: VerdictCache | None = None,
                 timeout: float = VIDEO_TIMEOUT):
        self.images = images
        self.cache = cache if cache is not None else images.cache
        self.timeout = timeout
        self._available = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = available() and self.images.available
        return self._available

    def cached(self, clip_key: str) -> ImageVerdict | None:
        return self.cache.get_video(clip_key)

    def judge_bytes(self, data: bytes, clip_key: str) -> ImageVerdict | None:
        """The whole judgement on the calling thread (a worker's): sample,
        judge each frame, combine, cache."""
        started = time.monotonic()
        try:
            frames = sample_frames(data)
        except Exception:  # noqa: BLE001 - unreadable is not a verdict
            log.debug("could not sample the clip", exc_info=True)
            return None
        if not frames:
            return None
        verdict = combine(self.images.judge_bytes(frame) for frame in frames)
        if verdict is None:
            return None
        self.cache.put_video(clip_key, verdict)
        log.info("judged a clip from %d frame(s) in %.0f ms: %s", len(frames),
                 (time.monotonic() - started) * 1000, verdict.level)
        return verdict

    def verdict(self, data: bytes, clip_key: str) -> ImageVerdict | None:
        """Judge a clip, waiting up to the deadline."""
        cached = self.cached(clip_key)
        if cached is not None:
            return cached
        if not self.available:
            return None
        future = self.images._executor().submit(self.judge_bytes, data, clip_key)
        try:
            return future.result(timeout=self.timeout)
        except Exception:  # noqa: BLE001 - includes the timeout
            return None

    async def verdict_async(self, data: bytes, clip_key: str) -> ImageVerdict | None:
        """The same, without holding the event loop; the worker finishes
        and caches even when the deadline passes."""
        import asyncio

        cached = self.cached(clip_key)
        if cached is not None:
            return cached
        if not self.available:
            return None
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self.images._executor(), self.judge_bytes,
                                      data, clip_key)
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except asyncio.TimeoutError:
            log.info("a clip took longer than %.0f s to judge; refused", self.timeout)
            return None
        except Exception:  # noqa: BLE001
            log.debug("clip judgement failed", exc_info=True)
            return None


__all__ = ["CLEAN", "VideoChecker", "available", "combine", "key", "sample_frames"]
