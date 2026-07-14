# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Render a loop run as ONE self-contained HTML page.

The loop's most valuable artifact isn't the final image — it's the evidence of how
it got there: what was tried, what was kept, what was thrown away. That story lives
in the ledger, but a JSON file convinces nobody.

Everything is inlined as base64, so the page renders with ComfyUI off, on someone
else's machine, in an email, or dropped on a website. A report that depends on a
running server is a report you can't show anyone.
"""

from __future__ import annotations

import base64
import html
import io
from typing import Any

from PIL import Image as PILImage

_CSS = """
:root{--cy:#00c8ff;--ink:#0a0d12;--card:#141920;--fg:#e8edf2;--mut:#8b959f;--bad:#ff6b6b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--ink);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:40px 24px}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:30px;font-weight:800;letter-spacing:-.5px;margin-bottom:6px}
h1 span{color:var(--cy)}
.brief{color:var(--mut);font-size:17px;margin-bottom:4px}
.gate{display:inline-block;margin-top:10px;font:13px ui-monospace,monospace;color:var(--cy);
  border:1px solid rgba(0,200,255,.35);border-radius:6px;padding:4px 10px}
.meta{margin-top:14px;color:var(--mut);font:13px ui-monospace,monospace}
hr{border:0;border-top:1px solid #222a33;margin:28px 0}
.pass{display:grid;grid-template-columns:150px 1fr;gap:22px;padding:18px 0;border-bottom:1px solid #1c232c;align-items:start}
.thumb{width:150px;border-radius:8px;border:1px solid #263039;display:block}
.thumb.none{height:100px;background:#11161c;display:flex;align-items:center;justify-content:center;color:#4a5560;font-size:12px}
.n{font:12px ui-monospace,monospace;color:var(--mut);letter-spacing:1px;text-transform:uppercase}
.change{font-size:17px;font-weight:600;margin:4px 0 8px}
.tag{display:inline-block;font:12px ui-monospace,monospace;font-weight:700;padding:3px 9px;border-radius:5px}
.kept{background:rgba(0,200,255,.14);color:var(--cy);border:1px solid rgba(0,200,255,.4)}
.rev{background:rgba(255,107,107,.12);color:var(--bad);border:1px solid rgba(255,107,107,.35)}
.score{font:12px ui-monospace,monospace;color:var(--mut);margin-left:8px}
.note{color:var(--mut);margin-top:6px;font-size:14px}
.final{margin-top:34px;padding:24px;background:var(--card);border:1px solid rgba(0,200,255,.25);border-radius:12px}
.final h2{font-size:13px;letter-spacing:2px;text-transform:uppercase;color:var(--cy);margin-bottom:14px}
.final img{max-width:100%;border-radius:8px;display:block}
.log{margin-top:34px}
.log h2{font-size:13px;letter-spacing:2px;text-transform:uppercase;color:var(--mut);margin-bottom:10px}
pre{background:#0f141a;border:1px solid #1c232c;border-radius:8px;padding:16px;overflow-x:auto;
  font:13px/1.7 ui-monospace,monospace;color:#c3ccd5}
.foot{margin-top:34px;color:#4a5560;font-size:12px;text-align:center}
"""


def _thumb(data: bytes, max_w: int = 300) -> str:
    """Downscale + inline as a data URI. A 20 MB report is a report nobody opens."""
    img = PILImage.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def render(run: dict[str, Any], images: dict[int, bytes], final: bytes | None = None) -> str:
    """`images` maps pass number -> raw image bytes (may be sparse)."""
    e = html.escape
    best = run.get("best") or {}
    best_n = best.get("pass")

    rows = []
    for p in run.get("passes", []):
        n = p["n"]
        raw = images.get(n)
        thumb = (
            f'<img class="thumb" src="{_thumb(raw)}" alt="pass {n}">'
            if raw
            else '<div class="thumb none">no output</div>'
        )
        kept = p.get("kept")
        tag = (
            '<span class="tag kept">✓ KEPT — new best</span>'
            if kept
            else f'<span class="tag rev">✗ {e(str(p.get("outcome", "")).upper())} — reverted</span>'
        )
        score = (
            f'<span class="score">score {p["score"]}</span>' if p.get("score") is not None else ""
        )
        note = f'<div class="note">{e(p["note"])}</div>' if p.get("note") else ""
        crown = " · FINAL" if n == best_n else ""
        rows.append(
            f'<div class="pass">{thumb}<div>'
            f'<div class="n">pass {n}{crown}</div>'
            f'<div class="change">{e(p.get("change", ""))}</div>'
            f"{tag}{score}{note}</div></div>"
        )

    log = []
    for p in run.get("passes", []):
        mark = "✓ kept" if p.get("kept") else f"✗ {p.get('outcome')}"
        sc = f" [score {p['score']}]" if p.get("score") is not None else ""
        log.append(f"pass {p['n']}  {p.get('change','')}{sc}  → {mark}")

    final_html = ""
    if final:
        final_html = (
            f'<div class="final"><h2>Final — pass {best_n}</h2>'
            f'<img src="{_thumb(final, 1000)}" alt="final result"></div>'
        )

    kept_n = sum(1 for p in run.get("passes", []) if p.get("kept"))
    total = len(run.get("passes", []))
    gate = f'<div class="gate">objective gate: {e(run["gate"])}</div>' if run.get("gate") else ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loop run {e(run.get('run_id',''))}</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>The loop, <span>pass by pass</span></h1>
<div class="brief">{e(run.get('brief',''))}</div>
{gate}
<div class="meta">run {e(run.get('run_id',''))} · {total} passes · {kept_n} kept · {total-kept_n} reverted · {e(run.get('status',''))}</div>
<hr>
{''.join(rows) or '<p class="note">No passes recorded.</p>'}
{final_html}
<div class="log"><h2>Loop log</h2><pre>{e(chr(10).join(log))}</pre></div>
<div class="foot">Generated by comfy-mcp — build → run → look → critique → fix.</div>
</div></body></html>"""
