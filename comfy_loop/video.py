# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Looking tools for VIDEO outputs — the half of the loop that stopped at mp4.

`get_result` already reports `gifs` and `videos` alongside `images`, so a video
workflow hands the model a filename. Every LOOK tool downstream of that was
Pillow-only, and Pillow cannot decode an mp4 — so for VHS / AnimateDiff / WAN
graphs the loop's central instruction ("call get_image on each and LOOK") was
unexecutable. The model got a filename it could not see, and judgement collapsed
back to the vibes the ratchet exists to prevent.

This restores the loop for video by extracting frames, which are images again.

WHY FFMPEG IS FAIR GAME HERE. imaging.py stays Pillow-only on purpose — an MCP
client has no shell. The difference is that anything producing these outputs
already shipped ffmpeg: VHS_VideoCombine shells out to it, so an install that
can emit an mp4 can decode one. If it is genuinely missing we say so plainly
rather than failing with a decode error.

THE TRAP THIS EXISTS TO AVOID. Comparing two clips by TIMESTAMP is wrong the
moment their lengths differ — a frame cap, a trimmed input or a different fps
lands you on different moments, and you end up "comparing" two unrelated
expressions and drawing a confident conclusion from it. Everything here indexes
by FRAME NUMBER, and `compare_video_frames` refuses quietly-wrong comparisons by
flagging a frame-count mismatch instead of rendering it anyway.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any

from PIL import Image as PILImage
from PIL import ImageChops, ImageStat


_WORK_WIDTH = 256  # frames are measured downscaled; roi is scaled to match


class VideoToolMissing(RuntimeError):
    """ffmpeg/ffprobe not on PATH — raised with a message meant for the model."""


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise VideoToolMissing(
            f"{tool} is not on PATH, so video frames cannot be decoded. "
            "ComfyUI's own video nodes (VHS_VideoCombine) shell out to ffmpeg, so an "
            "install that can WRITE these files can normally read them too — check the "
            "PATH this MCP server runs under rather than assuming ffmpeg is absent."
        )
    return path


def _tempfile(data: bytes, suffix: str = ".mp4") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def probe(data: bytes) -> dict[str, Any]:
    """Dimensions, fps and frame count — what you need before indexing frames.

    frame_count is reported as `estimated: true` when the container has no
    nb_frames and it had to be derived from duration x fps. That matters for the
    mismatch guard: an estimate off by a frame or two is fine for picking a
    sample point, but do not treat it as proof two clips are the same length.
    """
    _require("ffprobe")
    path = _tempfile(data)
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        info = json.loads(out.stdout or "{}").get("streams", [{}])[0]
    finally:
        os.unlink(path)

    num, _, den = (info.get("r_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    duration = float(info.get("duration") or 0) or 0.0
    raw_count = info.get("nb_frames")
    estimated = raw_count in (None, "", "N/A")
    count = int(round(duration * fps)) if estimated else int(raw_count)

    return {
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "fps": round(fps, 3),
        "frame_count": count,
        "frame_count_estimated": estimated,
        "duration_s": round(duration, 3),
    }


def frame(data: bytes, index: int = 0) -> bytes:
    """Extract ONE frame by index (0-based) as PNG bytes.

    Indexed by frame, never by timestamp — see the module docstring. Decoding
    walks from the start, so a very high index on a long clip is slow but exact;
    that trade is deliberate, because seeking by time is what produces the
    wrong-moment comparisons this module exists to prevent.
    """
    _require("ffmpeg")
    path = _tempfile(data)
    try:
        out = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-i", path,
                "-vf", f"select=gte(n\\,{max(0, int(index))})",
                "-frames:v", "1", "-f", "image2", "-vcodec", "png", "-",
            ],
            capture_output=True, timeout=300,
        )
        if not out.stdout:
            err = (out.stderr or b"").decode("utf-8", "ignore")[:300]
            raise RuntimeError(
                f"No frame at index {index}. Check frame_count with video_info first. {err}"
            )
        return out.stdout
    finally:
        os.unlink(path)


def _frames(data: bytes, stride: int, limit: int, width: int) -> list[PILImage.Image]:
    """Decode a downscaled, strided sample in ONE pass — for the stats below.

    Frames are written as discrete files rather than piped. Piping concatenated
    PPMs and re-opening the same buffer looks tidier and is a trap: Pillow does
    not reliably advance the stream between images, so the read loop can spin
    forever on a stream it has already consumed. A temp dir costs a little I/O
    and terminates.
    """
    _require("ffmpeg")
    path = _tempfile(data)
    outdir = tempfile.mkdtemp(prefix="comfy_loop_frames_")
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-i", path,
                "-vf", f"select=not(mod(n\\,{max(1, stride)})),scale={width}:-2",
                "-vsync", "0", "-frames:v", str(limit),
                os.path.join(outdir, "f_%05d.png"),
            ],
            capture_output=True, timeout=600, check=False,
        )
        frames = []
        for name in sorted(os.listdir(outdir)):
            with PILImage.open(os.path.join(outdir, name)) as img:
                frames.append(img.convert("RGB"))
        return frames
    finally:
        os.unlink(path)
        shutil.rmtree(outdir, ignore_errors=True)


def temporal_stats(
    data: bytes,
    stride: int = 1,
    max_frames: int = 120,
    roi: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Frame-to-frame instability, as a number the ratchet cannot kid itself about.

    This is the video equivalent of diff_stats: the objective gate for "does it
    boil?" — per-frame face swaps, temporal smoothing and denoisers all live or
    die on it, and it is precisely the defect a single still cannot show.

    HONEST LIMIT: this is a naive consecutive-frame difference, so REAL MOTION
    COUNTS AS INSTABILITY. A static locked-off shot gives a clean read; a moving
    subject does not. Two ways to get a meaningful number anyway, in order of
    preference:

      1. Compare the SAME clip before and after your change. The motion is
         identical in both, so any delta is your change. This is the honest
         primary use.
      2. Pass `roi` (left, top, right, bottom) in SOURCE pixels over a region that should be
         static — background, a wall, a plate area outside the composite. Then
         any energy at all is drift you did not intend, which is exactly the
         "did the background stay put?" question difference-over-gray answers
         for stills.

    A motion-compensated version (warp frame t-1 into t by optical flow, then
    difference) separates real motion from flicker properly, but needs numpy and
    an optical-flow implementation. Left out to keep the install light — and this
    is sufficient for the before/after comparison that actually drives the loop.
    """
    frames = _frames(data, stride=stride, limit=max_frames, width=_WORK_WIDTH)
    if len(frames) < 2:
        return {"frames_sampled": len(frames), "error": "need at least 2 frames to measure"}

    # roi arrives in SOURCE pixels — the coordinates you'd read off a frame — and
    # is scaled to the working size here. Taking it in working-space coordinates
    # instead would be a silent trap: the numbers would look reasonable and
    # measure the wrong part of the picture.
    if roi:
        src_w = probe(data)["width"] or _WORK_WIDTH
        k = _WORK_WIDTH / float(src_w)
        box = tuple(int(round(v * k)) for v in roi)
        frames = [f.crop(box) for f in frames]

    diffs = []
    for a, b in zip(frames, frames[1:]):
        d = ImageChops.difference(a.convert("L"), b.convert("L"))
        diffs.append(ImageStat.Stat(d).mean[0])

    mean = sum(diffs) / len(diffs)
    return {
        "frames_sampled": len(frames),
        "stride": stride,
        "roi": list(roi) if roi else None,
        "mean_frame_delta": round(mean, 4),
        "peak_frame_delta": round(max(diffs), 4),
        "note": (
            "Lower is steadier. Real motion counts toward this, so compare it "
            "against the SAME clip before your change, or restrict it to a static "
            "roi — never read one number in isolation."
        ),
    }
