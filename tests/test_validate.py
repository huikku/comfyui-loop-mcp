# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Offline test: the pre-flight has to separate "install something" from "fix the graph".

`/prompt` reports both as the same red box, one per submit. The whole point of
checking first is that the caller learns what to DO — download a model, install a
pack, re-wire a node — in one answer, before a GPU minute is spent. A checker
that finds problems but mislabels them is worse than none: it sends the model off
installing a pack when the checkpoint filename was simply misspelled.

Run:  python tests/test_validate.py     (no ComfyUI needed)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfy_loop.validate import validate

OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["sd15.safetensors", "sdxl.safetensors"]]}},
        "output": ["MODEL", "CLIP", "VAE"],
    },
    "KSampler": {
        "input": {"required": {
            "model": ["MODEL"],
            "seed": ["INT", {"control_after_generate": True}],
            "steps": ["INT", {"min": 1, "max": 100}],
            "cfg": ["FLOAT", {"min": 0.0, "max": 30.0}],
            "latent_image": ["LATENT"],
        }},
    },
    "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True},
}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def problems(result, key="blockers"):
    return [f"{f['class']}.{f['input']}: {f['problem']}" for f in result[key]]


# A graph that is fine must come back clean — a checker that cries wolf gets ignored.
good = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
    "2": {"class_type": "KSampler", "inputs": {
        "model": ["1", 0], "seed": 42, "steps": 20, "cfg": 7.0, "latent_image": ["1", 0]}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
}
res = validate(good, OBJECT_INFO)
check("a valid graph reports nothing", res["blockers"] == [] and res["warnings"] == [],
      str(problems(res) + problems(res, "warnings")))
check("output nodes are counted", res["output_nodes"] == 1)

# A model filename that isn't installed is a DOWNLOAD, and the fix should name
# what this box does have — the most common real failure, and the one most often
# misread as a broken graph.
bad_model = dict(good, **{"1": {"class_type": "CheckpointLoaderSimple",
                               "inputs": {"ckpt_name": "sd15.ckpt"}}})
res = validate(bad_model, OBJECT_INFO)
check("an uninstalled model file is a blocker",
      any(b["input"] == "ckpt_name" for b in res["blockers"]), str(problems(res)))
check("and it is reported as a missing FILE, not a missing node",
      res["missing_files"] and not res["missing_classes"], str(res["missing_classes"]))
check("the fix names the nearest thing actually installed",
      any("sd15.safetensors" in b["fix"] for b in res["blockers"]), str(problems(res)))

# A class nobody installed is a PACK install, and must not be confused with the above.
bad_class = dict(good, **{"4": {"class_type": "UltimateSDUpscale", "inputs": {}}})
res = validate(bad_class, OBJECT_INFO)
check("an unknown node class is a blocker",
      "UltimateSDUpscale" in res["missing_classes"], str(res["missing_classes"]))
check("and it is not reported as a missing file", res["missing_files"] == [])

# Graph-shaped mistakes: a required input never set, a wire to a node that is gone.
holes = {
    "2": {"class_type": "KSampler", "inputs": {"model": ["99", 0], "seed": 1, "steps": 20, "cfg": 7.0}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
}
res = validate(holes, OBJECT_INFO)
check("a missing required input is caught",
      any("required" in b["problem"] and b["input"] == "latent_image" for b in res["blockers"]),
      str(problems(res)))
check("a wire pointing at a node that isn't in the graph is caught",
      any("not in the graph" in b["problem"] for b in res["blockers"]), str(problems(res)))

# Out-of-range is a warning, not a blocker: the node may well clamp it.
res = validate(dict(good, **{"2": {"class_type": "KSampler", "inputs": {
    "model": ["1", 0], "seed": 1, "steps": 5000, "cfg": 7.0, "latent_image": ["1", 0]}}}), OBJECT_INFO)
check("an out-of-range value warns rather than blocks",
      res["blockers"] == [] and any("above the declared max" in w["problem"] for w in res["warnings"]),
      str(problems(res) + problems(res, "warnings")))

# A graph with nothing to look at is the loop's own failure mode.
res = validate({"1": {"class_type": "CheckpointLoaderSimple",
                      "inputs": {"ckpt_name": "sd15.safetensors"}}}, OBJECT_INFO)
check("a graph with no output node is flagged",
      any("no output node" in w["problem"] for w in res["warnings"]), str(problems(res, "warnings")))

# Handing it litegraph by mistake must say so, not report 40 phantom problems.
res = validate({"nodes": [], "links": []}, OBJECT_INFO)
check("litegraph passed by mistake is named as such",
      any("API format" in b["fix"] or "litegraph" in b["fix"] for b in res["blockers"]),
      str(problems(res)))

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all validate tests passed")
