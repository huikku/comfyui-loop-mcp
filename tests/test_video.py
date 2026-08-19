# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Offline tests for the video LOOK tools.

No ComfyUI needed — clips are synthesized with ffmpeg, so this runs anywhere the
tools themselves would work. If ffmpeg is missing the tests skip rather than
fail, because that is the same condition the tools degrade on.

The cases worth having:
  - frame() indexes by FRAME, and different indices really are different frames.
    Indexing by timestamp is the bug this module exists to prevent, and a test
    that only checks "returns a PNG" would pass while indexing was broken.
  - temporal_stats() ranks a static clip below a moving one, i.e. the number
    means what it claims.
  - temporal_stats() TERMINATES. The first implementation piped concatenated
    PPMs and re-opened one buffer in a loop; Pillow does not reliably advance
    the stream, so it spun forever. A hang is worse than a wrong answer — it
    looks like a crashed client.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from comfy_loop import imaging, video  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def _synth(filter_str: str) -> bytes:
    """Render a tiny clip from a full lavfi filter string and return its bytes.

    The caller passes the whole spec because the separator differs per source:
    `testsrc=s=...` but `color=c=navy:s=...`. Building it here got that wrong.
    """
    path = os.path.join(tempfile.mkdtemp(), "c.mp4")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
         "-i", filter_str, "-pix_fmt", "yuv420p", path],
        check=True, capture_output=True, timeout=120,
    )
    data = open(path, "rb").read()
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    return data


def main() -> int:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("SKIP  ffmpeg/ffprobe not on PATH — video tools would degrade, not fail")
        return 0

    moving = _synth("testsrc=s=64x64:d=1:r=10")
    static = _synth("color=c=navy:s=64x64:d=1:r=10")

    info = video.probe(moving)
    check(info["width"] == 64 and info["height"] == 64, "probe reports dimensions")
    check(abs(info["fps"] - 10) < 0.5, "probe reports fps")
    check(info["frame_count"] >= 9, f"probe reports frame count ({info['frame_count']})")

    f0, f5 = video.frame(moving, 0), video.frame(moving, 5)
    png = bytes([137, 80, 78, 71, 13, 10, 26, 10])
    check(f0[:8] == png and f5[:8] == png, "frame() returns PNG bytes")
    check(f0 != f5, "frame() indexes by FRAME — different indices differ")

    # A frame beyond the end must fail loudly, not hand back the last frame as if
    # it were the one asked for.
    try:
        video.frame(moving, 10_000)
        check(False, "frame() past the end raises")
    except RuntimeError:
        check(True, "frame() past the end raises")

    s_moving = video.temporal_stats(moving, max_frames=10)
    s_static = video.temporal_stats(static, max_frames=10)
    check("mean_frame_delta" in s_moving, "temporal_stats returns a score")
    check(
        s_static["mean_frame_delta"] < s_moving["mean_frame_delta"],
        f"temporal_stats ranks static ({s_static['mean_frame_delta']}) "
        f"below moving ({s_moving['mean_frame_delta']})",
    )
    check(s_static["mean_frame_delta"] < 1.0, "a static clip scores near zero")

    roi = video.temporal_stats(moving, max_frames=6, roi=(0, 0, 32, 32))
    check(roi["roi"] == [0, 0, 32, 32], "temporal_stats honours an roi")

    short = video.temporal_stats(_synth("color=c=red:s=64x64:d=1:r=1"), max_frames=10)
    check("error" in short or short["frames_sampled"] >= 1, "single-frame clip degrades cleanly")

    tall = imaging.annotate(imaging.side_by_side(f0, f5), "WARNING: lengths differ")
    from PIL import Image as PILImage
    import io
    before = PILImage.open(io.BytesIO(imaging.side_by_side(f0, f5))).height
    after = PILImage.open(io.BytesIO(tall)).height
    check(after > before, "annotate adds a visible warning bar")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: " + "; ".join(FAILED))
        return 1
    print("all video tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
