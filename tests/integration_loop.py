# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Integration test for the 18 tools integration_smoke.py doesn't reach.

integration_smoke covers discovery plus a single generation. This covers what
makes this an MCP for *looping* rather than for driving ComfyUI once: the durable
ratchet, the LOOK tools (compare / measure / diff), graph conversion, upload,
run_template and interrupt.

It runs a real two-pass loop, not a set of isolated calls. The load-bearing check
is that **an objective score overrules a wrong verdict**: pass 2 is recorded as
"better" while being handed a worse score, and the ledger must reject the claim
and keep pass 1 as best, graph included. A ratchet that believes whatever the
agent tells it is not a ratchet — and in a long run the agent is exactly the thing
that goes wrong.

Non-mutating: installs nothing, restarts nothing (see README for those). Loop
state goes to a temp dir, never to the real ~/.comfy-mcp.

Run:  COMFYUI_URL=http://localhost:8188 python tests/integration_loop.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
CKPT = os.environ.get("TEST_CKPT", "v1-5-pruned-emaonly.safetensors")
results: list[tuple[str, bool, str]] = []


def txt(res) -> str:
    return "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")


def has_image(res) -> bool:
    return any(getattr(c, "type", "") == "image" for c in res.content)


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def as_json(blob: str) -> dict:
    """Tools that return JSON wrap it in nothing; be forgiving anyway."""
    try:
        return json.loads(blob[blob.index("{"):blob.rindex("}") + 1])
    except Exception:  # noqa: BLE001
        return {}


def flowzip_body(blob: str) -> str:
    """get_template prefixes a prose header; the FlowZip itself starts at 'W:'."""
    for i, line in enumerate(blob.splitlines()):
        if line.startswith("W:"):
            return "\n".join(blob.splitlines()[i:])
    return blob


def graph(steps: int, seed: int = 1) -> dict:
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red apple on a wooden table, studio photo", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": 7.0,
              "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
              "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "looptest"}},
    }


async def render(s, g: dict) -> dict | None:
    sub = txt(await s.call_tool("submit_workflow", {"workflow": g}))
    if "prompt_id=" not in sub:
        return None
    pid = sub.split("prompt_id=")[1].split()[0].strip()
    gr = txt(await s.call_tool("get_result", {"prompt_id": pid, "timeout_s": 240}))
    try:
        return json.loads(gr[gr.index("["):gr.index("]") + 1])[0]
    except Exception:  # noqa: BLE001
        return None


async def main() -> int:
    state = tempfile.mkdtemp(prefix="comfy-mcp-looptest-")
    params = StdioServerParameters(
        command="comfy-mcp",
        env={**os.environ, "COMFYUI_URL": URL, "COMFY_MCP_STATE_DIR": state},
    )
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                # ---------------- upload ----------------
                from PIL import Image
                probe = Path(tempfile.gettempdir()) / "comfy_mcp_upload_probe.png"
                Image.new("RGB", (64, 64), (200, 30, 30)).save(probe)
                up = txt(await s.call_tool("upload_image", {"path": str(probe)}))
                check("upload_image", "Uploaded as" in up, up.strip().splitlines()[0][:60])

                # ---------------- graph conversion ----------------
                fz = flowzip_body(txt(await s.call_tool(
                    "get_template", {"name": "default", "source": "online"})))
                api = txt(await s.call_tool("flowzip_to_api", {"flowzip": fz}))
                check("flowzip_to_api -> API format", '"class_type"' in api, api.strip()[:50])
                infl = txt(await s.call_tool("inflate_workflow", {"flowzip": fz}))
                check("inflate_workflow -> litegraph", '"nodes"' in infl, infl.strip()[:50])

                # ---------------- run_template with overrides ----------------
                # The catalog's 'default' names a checkpoint this box may not have,
                # so override it — which is exactly what overrides are for.
                rt = txt(await s.call_tool("run_template", {
                    "name": "default", "source": "online",
                    "overrides": {"4": {"ckpt_name": CKPT},
                                  "3": {"steps": 8},
                                  "6": {"text": "a single ripe pear, studio photo"}}}))
                check("run_template submits", "prompt_id" in rt, rt.strip().splitlines()[0][:70])
                if "prompt_id=" in rt:
                    pid = rt.split("prompt_id=")[1].split()[0].strip()
                    gr = txt(await s.call_tool("get_result", {"prompt_id": pid, "timeout_s": 300}))
                    check("run_template produces an image", ".png" in gr, gr.strip().splitlines()[0][:70])

                # ---------------- two real passes ----------------
                a = await render(s, graph(steps=6))
                b = await render(s, graph(steps=20))
                check("two passes rendered", bool(a and b))
                if not (a and b):
                    return 1

                # ---------------- LOOK ----------------
                m1 = as_json(txt(await s.call_tool("measure_image", {"filename": a["filename"], "metric": "sharpness"})))
                m2 = as_json(txt(await s.call_tool("measure_image", {"filename": b["filename"], "metric": "sharpness"})))
                s1, s2 = m1.get("sharpness"), m2.get("sharpness")
                check("measure_image returns a sharpness score",
                      isinstance(s1, (int, float)) and isinstance(s2, (int, float)),
                      f"6-step={s1}  20-step={s2}")

                check("compare_images side_by_side returns an image", has_image(await s.call_tool(
                    "compare_images", {"filename_a": a["filename"], "filename_b": b["filename"],
                                       "mode": "side_by_side"})))
                check("compare_images difference returns an image", has_image(await s.call_tool(
                    "compare_images", {"filename_a": a["filename"], "filename_b": b["filename"],
                                       "mode": "difference"})))
                ds = as_json(txt(await s.call_tool("image_diff_stats", {
                    "filename_a": a["filename"], "filename_b": b["filename"]})))
                check("image_diff_stats reports a real difference",
                      "mean_abs_diff" in ds and ds.get("identical") is False,
                      f"mean_abs_diff={ds.get('mean_abs_diff')}")

                # ---------------- the ratchet ----------------
                ls = txt(await s.call_tool("loop_start", {
                    "brief": "a crisp studio photo of a red apple",
                    "gate": "sharpness must not regress"}))
                run_id = ls.split("run_id:")[1].split()[0].strip() if "run_id:" in ls else ""
                check("loop_start returns a run_id", bool(run_id), run_id)
                if not run_id:
                    return 1

                r1 = txt(await s.call_tool("loop_record", {
                    "run_id": run_id, "change": "steps 6 (baseline)", "outcome": "better",
                    "graph": graph(6), "score": s1}))
                check("loop_record stores pass 1 as best", "NEW BEST" in r1, r1.strip().splitlines()[0][:60])

                # THE test: claim "better" while handing over a WORSE score.
                r2 = txt(await s.call_tool("loop_record", {
                    "run_id": run_id, "change": "steps 20", "outcome": "better",
                    "graph": graph(20), "score": s1 - 1.0,
                    "note": "agent claims better; the score disagrees"}))
                check("objective score OVERRULES a wrong 'better' verdict",
                      "worse" in r2.lower() and "REVERT" in r2, r2.strip().splitlines()[0][:60])
                check("the revert path hands back the BEST GRAPH", "BEST GRAPH" in r2 and '"steps": 6' in r2,
                      "without the graph, 'revert' is advice you cannot act on")

                best = txt(await s.call_tool("loop_best", {"run_id": run_id}))
                check("loop_best still returns pass 1", "pass 1" in best and '"steps": 6' in best,
                      best.strip().splitlines()[0][:60])

                led = txt(await s.call_tool("loop_ledger", {"run_id": run_id}))
                check("loop_ledger shows kept AND reverted",
                      "✓ kept" in led and "✗ worse" in led, led.strip().splitlines()[0][:60])

                rep = txt(await s.call_tool("loop_report", {"run_id": run_id}))
                path = next((w for w in rep.split() if w.endswith(".html")), "")
                html = Path(path).read_text() if path and Path(path).exists() else ""
                check("loop_report writes a self-contained HTML page",
                      "<html" in html.lower() and "http://" not in html.split("<body")[0],
                      f"{len(html)} bytes at {path}")

                fin = txt(await s.call_tool("loop_finish", {
                    "run_id": run_id, "summary": "20 steps did not beat 6 on sharpness"}))
                check("loop_finish converges and asks for sign-off", "CONVERGED" in fin,
                      fin.strip().splitlines()[0][:60])

                # durability is the entire point — it must survive context compaction
                persisted = list(Path(state).rglob("*.json"))
                check("loop state persisted to disk", bool(persisted), f"{len(persisted)} file(s)")

                # ---------------- interrupt ----------------
                await s.call_tool("submit_workflow", {"workflow": graph(steps=40, seed=99)})
                it = txt(await s.call_tool("interrupt", {}))
                check("interrupt is accepted", "Interrupt sent" in it, it.strip()[:50])

                # ---------------- save_workflow ----------------
                sw = txt(await s.call_tool("save_workflow", {
                    "workflow": graph(6), "name": "mcp_looptest", "save": False}))
                check("save_workflow round-trip verifies", "Round-trip verified" in sw,
                      sw.strip().splitlines()[0][:70])
    finally:
        shutil.rmtree(state, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} — {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
