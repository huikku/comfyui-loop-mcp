"""Looking tools — the comparisons that turn "looks fine" into a named defect.

The loop prompt tells the model to view outputs side-by-side and to difference
them over 50% gray. Through an MCP client there is no shell to run ffmpeg, so
without these the advice is unexecutable. Pillow only — no numpy — to keep the
install light.

Two kinds of looking:
  - compare()  -> an IMAGE the model views (side-by-side, or difference-over-gray
                  where identical regions read flat mid-gray and only real changes pop)
  - measure()  -> NUMBERS, for the cases where the brief has an objective gate
                  (does this texture actually tile? did the upscale add detail or
                  just soften?) — the ratchet needs a score it can't kid itself about.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image as PILImage
from PIL import ImageChops, ImageFilter, ImageStat


def _open(data: bytes) -> PILImage.Image:
    return PILImage.open(io.BytesIO(data)).convert("RGB")


def _encode(img: PILImage.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _match_size(a: PILImage.Image, b: PILImage.Image) -> tuple[PILImage.Image, PILImage.Image]:
    """Differencing needs identical dimensions; scale b to a rather than refuse."""
    if a.size != b.size:
        b = b.resize(a.size, PILImage.LANCZOS)
    return a, b


def side_by_side(a: bytes, b: bytes, gap: int = 12) -> bytes:
    """Two images on one canvas — the fastest way to see what moved."""
    ia, ib = _open(a), _open(b)
    h = max(ia.height, ib.height)
    w = ia.width + gap + ib.width
    canvas = PILImage.new("RGB", (w, h), (20, 20, 20))
    canvas.paste(ia, (0, 0))
    canvas.paste(ib, (ia.width + gap, 0))
    return _encode(canvas)


def difference(a: bytes, b: bytes, amplify: float = 1.0) -> bytes:
    """0.5 + 0.5*(a-b): identical regions read flat mid-gray, changes pop.

    The fastest way to answer "did the background actually stay put?" — which the
    eye is bad at and this is good at.
    """
    ia, ib = _match_size(_open(a), _open(b))
    diff = ImageChops.difference(ia, ib)
    if amplify != 1.0:
        diff = diff.point(lambda v: min(255, int(v * amplify)))
    gray = PILImage.new("RGB", ia.size, (128, 128, 128))
    return _encode(ImageChops.add(diff, gray))


def diff_stats(a: bytes, b: bytes, threshold: int = 8) -> dict[str, Any]:
    """How much changed, objectively — the 'I changed only what I meant to' gate."""
    ia, ib = _match_size(_open(a), _open(b))
    diff = ImageChops.difference(ia, ib).convert("L")
    stat = ImageStat.Stat(diff)
    hist = diff.histogram()
    total = sum(hist)
    changed = sum(hist[threshold:])
    return {
        "mean_abs_diff": round(stat.mean[0], 3),
        "max_abs_diff": diff.getextrema()[1],
        "pct_pixels_changed": round(100.0 * changed / total, 2) if total else 0.0,
        "identical": changed == 0,
    }


def sharpness(data: bytes) -> float:
    """Edge energy. Higher = more real detail.

    The upscale/restore gate: 'crisper' and 'hallucinated' both look different from
    the source, but only one of them raises this. Compare across passes, not to an
    absolute — it's scale- and content-dependent.
    """
    img = _open(data).convert("L")
    edges = img.filter(ImageFilter.FIND_EDGES)
    return round(ImageStat.Stat(edges).stddev[0], 3)


def brightness(data: bytes) -> dict[str, Any]:
    img = _open(data).convert("L")
    stat = ImageStat.Stat(img)
    hist = img.histogram()
    total = sum(hist)
    cum, p99 = 0, 255
    for v, count in enumerate(hist):
        cum += count
        if cum >= 0.99 * total:
            p99 = v
            break
    return {"mean": round(stat.mean[0], 2), "stddev": round(stat.stddev[0], 2), "p99": p99}


def tile_seam(data: bytes, strip: int = 8) -> dict[str, Any]:
    """Does this texture ACTUALLY tile? The objective gate for 'seamless'.

    Compare the wrap-around join (right edge against left edge, bottom against
    top) with an interior join of the same width. A truly seamless tile makes the
    wrap look like any other interior transition, so the ratio lands near 1.0. A
    visible seam spikes it. This is exactly the kind of claim an eye — and a model
    eager to be done — will wave through, so score it.
    """
    img = _open(data)
    w, h = img.size
    s = max(1, min(strip, w // 4, h // 4))

    def edge_delta(left: PILImage.Image, right: PILImage.Image) -> float:
        d = ImageChops.difference(left, right).convert("L")
        return ImageStat.Stat(d).mean[0]

    # horizontal wrap: last strip vs first strip
    h_wrap = edge_delta(img.crop((w - s, 0, w, h)), img.crop((0, 0, s, h)))
    # interior baseline: two adjacent strips mid-image
    mid = w // 2
    h_base = edge_delta(img.crop((mid - s, 0, mid, h)), img.crop((mid, 0, mid + s, h)))

    v_wrap = edge_delta(img.crop((0, h - s, w, h)), img.crop((0, 0, w, s)))
    midy = h // 2
    v_base = edge_delta(img.crop((0, midy - s, w, midy)), img.crop((0, midy, w, midy + s)))

    eps = 0.5  # below this, a "difference" is noise, not signal

    def ratio(wrap: float, base: float) -> float:
        # A flat interior (uniform image along this axis) makes the baseline ~0. Dividing
        # by it reports every image as a seam, so decide on the wrap itself:
        if wrap <= eps:
            return 0.0  # no discontinuity at the join at all — tiles perfectly on this axis
        if base <= eps:
            return 99.0  # a jump at the wrap but a flat interior — unambiguously a seam
        return round(wrap / base, 2)

    hr, vr = ratio(h_wrap, h_base), ratio(v_wrap, v_base)
    worst = max(hr, vr)
    return {
        "h_seam_ratio": hr,
        "v_seam_ratio": vr,
        "score": round(-worst, 3),  # higher-is-better, for the ratchet
        "verdict": (
            "seamless" if worst < 1.3
            else "borderline" if worst < 2.0
            else "VISIBLE SEAM"
        ),
        "note": "ratio of the wrap-around join to an interior join; ~1.0 = tiles cleanly, >2 = a real seam",
    }
