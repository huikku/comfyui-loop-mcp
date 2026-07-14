# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Offline test: litegraph_to_api must resolve through Reroute nodes.

Reroute is a frontend-only passthrough with no backend class. A link pointing at
one has to be followed back to the real producer, or the API graph we hand to
/prompt references a node id that doesn't exist and the run dies with a
confusing error far from the cause.

Run:  python tests/test_reroute.py     (no ComfyUI needed)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfy_mcp.compress import litegraph_to_api

OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["m.safetensors"]]}}},
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING"], "clip": ["CLIP"]}}},
}

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def graph(nodes, links):
    return {"nodes": nodes, "links": links}


LOADER = {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["m.safetensors"],
          "inputs": [], "outputs": [{"name": "MODEL", "links": []}, {"name": "CLIP", "links": [1]}]}


def encoder(link):
    return {"id": 9, "type": "CLIPTextEncode", "widgets_values": ["hello"],
            "inputs": [{"name": "clip", "type": "CLIP", "link": link}], "outputs": []}


def reroute(nid, link):
    return {"id": nid, "type": "Reroute",
            "inputs": [{"name": "", "type": "*", "link": link}],
            "outputs": [{"name": "", "type": "CLIP", "links": []}]}


# 1) a single reroute between loader and consumer
api, warn = litegraph_to_api(
    graph([LOADER, reroute(5, 1), encoder(2)],
          [[1, 1, 1, 5, 0, "CLIP"], [2, 5, 0, 9, 0, "CLIP"]]),
    OBJECT_INFO)
check("single reroute resolves to the real producer",
      api["9"]["inputs"].get("clip") == ["1", 1], f'got {api["9"]["inputs"].get("clip")}')
check("reroute is not emitted as a node", "5" not in api)
check("no spurious warnings", not warn, str(warn))

# 2) a chain of them — the shape an actual bus produces
api, _ = litegraph_to_api(
    graph([LOADER, reroute(5, 1), reroute(6, 2), reroute(7, 3), encoder(4)],
          [[1, 1, 1, 5, 0, "CLIP"], [2, 5, 0, 6, 0, "CLIP"],
           [3, 6, 0, 7, 0, "CLIP"], [4, 7, 0, 9, 0, "CLIP"]]),
    OBJECT_INFO)
check("chained reroutes collapse to the original source",
      api["9"]["inputs"].get("clip") == ["1", 1], f'got {api["9"]["inputs"].get("clip")}')

# 3) a reroute wired to nothing must warn, not crash or emit a dangling ref
api, warn = litegraph_to_api(
    graph([LOADER, reroute(5, None), encoder(2)], [[2, 5, 0, 9, 0, "CLIP"]]),
    OBJECT_INFO)
check("dangling reroute leaves the input unwired", "clip" not in api["9"]["inputs"])
check("dangling reroute warns", any("dangling" in w for w in warn), str(warn))

# 4) a cycle must terminate rather than spin forever
api, warn = litegraph_to_api(
    graph([reroute(5, 2), reroute(6, 1), encoder(3)],
          [[1, 5, 0, 6, 0, "CLIP"], [2, 6, 0, 5, 0, "CLIP"], [3, 5, 0, 9, 0, "CLIP"]]),
    OBJECT_INFO)
check("cyclic reroute chain terminates", "clip" not in api["9"]["inputs"])

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all reroute tests passed")
