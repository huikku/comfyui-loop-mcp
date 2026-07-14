# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""The three tools that CHANGE the ComfyUI host: install_model, install_node_pack,
restart_comfyui.

These are gated, not skipped. They genuinely download files onto the box and
restart the server, so running them has to be a decision — but "it mutates" is a
reason to put a gate in front of a test, not a reason to ship the tool untested.

    COMFY_MCP_ALLOW_MUTATION=1 COMFYUI_URL=http://localhost:8188 \
        python tests/integration_mutating.py

Without the env var it refuses to run and tells you what it would have done.

What it does, and does NOT undo:
  - downloads a small upscale model (~67 MB) into models/upscale_models/
  - installs the rgthree-comfy node pack
  - restarts ComfyUI (twice)

Both artefacts are harmless and commonly wanted; neither is removed afterwards.
Do not point this at a machine you care about the state of.

The restart FAILURE path is covered non-destructively in integration_loop.py.
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
MODEL = os.environ.get("TEST_MODEL", "RealESRGAN x2")
PACK = os.environ.get("TEST_PACK", "rgthree-comfy")
results: list[tuple[str, bool, str]] = []


def txt(res) -> str:
    return "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def wait_up(s, tries: int = 40) -> bool:
    """ComfyUI is a big Python process; it does not come back instantly."""
    for _ in range(tries):
        await asyncio.sleep(3)
        if "up at" in txt(await s.call_tool("check_comfyui", {})):
            return True
    return False


async def main() -> int:
    if os.environ.get("COMFY_MCP_ALLOW_MUTATION") != "1":
        print("REFUSING TO RUN — this suite changes the ComfyUI host.\n")
        print(f"  It would install the model  : {MODEL}")
        print(f"  It would install the pack   : {PACK}")
        print( "  It would restart ComfyUI    : twice")
        print("\nSet COMFY_MCP_ALLOW_MUTATION=1 if that is what you want.")
        return 0

    params = StdioServerParameters(command="comfy-mcp", env={**os.environ, "COMFYUI_URL": URL})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            # ---------- install_model ----------
            # Ground truth is the loader's own enum, not the installer's say-so.
            before = txt(await s.call_tool("list_models", {
                "class_name": "UpscaleModelLoader", "input_name": "model_name"}))
            im = txt(await s.call_tool("install_model", {"name": MODEL}))
            check("install_model accepted", "install" in im.lower() or "queued" in im.lower(),
                  im.strip().splitlines()[0][:70])
            await asyncio.sleep(20)  # Manager downloads in the background
            after = txt(await s.call_tool("list_models", {
                "class_name": "UpscaleModelLoader", "input_name": "model_name"}))
            check("install_model — the file actually appears in the loader enum",
                  len(after) > len(before) or "realesrgan" in after.lower(),
                  "a 'success' message that leaves no file is the failure mode here")

            # ---------- install_node_pack + restart ----------
            ip = txt(await s.call_tool("install_node_pack", {"pack_id": PACK}))
            check("install_node_pack accepted", "install" in ip.lower() or "queued" in ip.lower(),
                  ip.strip().splitlines()[0][:70])

            rs = txt(await s.call_tool("restart_comfyui", {}))
            check("restart_comfyui reports the restart was triggered", "Restart triggered" in rs,
                  rs.strip().splitlines()[0][:60])
            check("ComfyUI comes back up", await wait_up(s))

            # The only claim that matters: are the pack's nodes now in /object_info?
            nodes = txt(await s.call_tool("list_nodes", {"keyword": "rgthree"}))
            check("install_node_pack — nodes register after the restart",
                  "rgthree" in nodes.lower(),
                  "a restart that doesn't register the pack is the whole reason this tool exists")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} — {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
