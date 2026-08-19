# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Offline test: subgraph instances must be EXPANDED, not skipped.

A subgraph is a canvas-only abstraction — `/prompt` has never heard of it. Skip
the instance and you don't get a smaller graph, you get one with a hole where
the pipe used to be: the consumer downstream references a node that no longer
exists, and the run fails somewhere far from the cause. Much of the current
template catalog is authored this way, so this is the difference between a
template running and not running at all.

Run:  python tests/test_subgraph.py     (no ComfyUI needed)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comfy_loop.compress import litegraph_to_api
from comfy_loop.subgraph import flatten

OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["m.safetensors"]]}},
        "output": ["MODEL", "CLIP"],
    },
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING"], "clip": ["CLIP"]}}},
    "CondConsumer": {"input": {"required": {"cond": ["CONDITIONING"]}}},
}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


LOADER = {
    "id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["m.safetensors"],
    "inputs": [], "outputs": [{"name": "MODEL", "links": []}, {"name": "CLIP", "links": [1]}],
}
CONSUMER = {
    "id": 3, "type": "CondConsumer",
    "inputs": [{"name": "cond", "type": "CONDITIONING", "link": 2}], "outputs": [],
}


def encode_def(sid: str = "SG1", promoted: bool = False) -> dict:
    """A definition holding one CLIPTextEncode: CLIP in, CONDITIONING out."""
    inner_inputs = [{"name": "clip", "type": "CLIP", "link": 11}]
    links = [[11, -10, 0, 5, 0, "CLIP"], [12, 5, 0, -20, 0, "CONDITIONING"]]
    sg_inputs = [{"name": "clip", "type": "CLIP"}]
    widgets = ["a cat"]
    if promoted:
        # The prompt is exposed on the instance instead of typed inside.
        inner_inputs = [
            {"name": "text", "type": "STRING", "link": 13},
            {"name": "clip", "type": "CLIP", "link": 11},
        ]
        links.append([13, -10, 1, 5, 0, "STRING"])
        sg_inputs.append({"name": "text", "type": "STRING", "widget": {"name": "text"}})
        widgets = []
    return {
        "id": sid, "name": "Encode",
        "inputs": sg_inputs,
        "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
        "nodes": [{
            "id": 5, "type": "CLIPTextEncode", "widgets_values": widgets,
            "inputs": inner_inputs, "outputs": [{"name": "CONDITIONING", "links": [12]}],
        }],
        "links": links,
    }


def instance(nid: int, sid: str = "SG1", link_in: int | None = 1, widgets=None) -> dict:
    return {
        "id": nid, "type": sid,
        "inputs": [{"name": "clip", "type": "CLIP", "link": link_in}],
        "outputs": [{"name": "CONDITIONING", "links": [2]}],
        "widgets_values": widgets or [],
    }


# --------------------------------------------------------------------------- #
# 1. The basic boundary: outer -> instance -> outer, both crossings survive
# --------------------------------------------------------------------------- #
wf = {
    "nodes": [LOADER, instance(2), CONSUMER],
    "links": [[1, 1, 1, 2, 0, "CLIP"], [2, 2, 0, 3, 0, "CONDITIONING"]],
    "definitions": {"subgraphs": [encode_def()]},
}
api, warns = litegraph_to_api(wf, OBJECT_INFO)
check("interior node is emitted, namespaced by its instance", "2:5" in api, str(sorted(api)))
check("no subgraph instance survives into the API graph",
      not any(n["class_type"] == "SG1" for n in api.values()))
check("inbound wire crosses the boundary (interior clip <- outer loader)",
      api.get("2:5", {}).get("inputs", {}).get("clip") == ["1", 1],
      str(api.get("2:5")))
check("outbound wire crosses the boundary (outer consumer <- interior node)",
      api.get("3", {}).get("inputs", {}).get("cond") == ["2:5", 0],
      str(api.get("3")))
check("interior widget value survives", api.get("2:5", {}).get("inputs", {}).get("text") == "a cat")
check("clean expansion reports no warnings", warns == [], str(warns))

# --------------------------------------------------------------------------- #
# 2. Two instances of one definition must not collide
# --------------------------------------------------------------------------- #
wf2 = {
    "nodes": [LOADER, instance(2), instance(4), CONSUMER],
    "links": [[1, 1, 1, 2, 0, "CLIP"], [3, 1, 1, 4, 0, "CLIP"], [2, 2, 0, 3, 0, "CONDITIONING"]],
    "definitions": {"subgraphs": [encode_def()]},
}
api2, _ = litegraph_to_api(wf2, OBJECT_INFO)
check("each instance gets its own copy of the interior",
      "2:5" in api2 and "4:5" in api2, str(sorted(api2)))
check("the second copy is wired independently",
      api2.get("4:5", {}).get("inputs", {}).get("clip") == ["1", 1])

# --------------------------------------------------------------------------- #
# 3. Nesting: a definition containing an instance of another definition
# --------------------------------------------------------------------------- #
outer_def = {
    "id": "SG_OUT", "name": "Outer",
    "inputs": [{"name": "clip", "type": "CLIP"}],
    "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
    "nodes": [{
        "id": 7, "type": "SG1",
        "inputs": [{"name": "clip", "type": "CLIP", "link": 21}],
        "outputs": [{"name": "CONDITIONING", "links": [22]}],
    }],
    "links": [[21, -10, 0, 7, 0, "CLIP"], [22, 7, 0, -20, 0, "CONDITIONING"]],
}
wf3 = {
    "nodes": [LOADER, instance(2, "SG_OUT"), CONSUMER],
    "links": [[1, 1, 1, 2, 0, "CLIP"], [2, 2, 0, 3, 0, "CONDITIONING"]],
    "definitions": {"subgraphs": [outer_def, encode_def()]},
}
api3, warns3 = litegraph_to_api(wf3, OBJECT_INFO)
check("nested instance expands through both levels", "2:7:5" in api3, str(sorted(api3)))
check("a wire crossing two boundaries still finds the real producer",
      api3.get("2:7:5", {}).get("inputs", {}).get("clip") == ["1", 1], str(api3.get("2:7:5")))
check("and the consumer two levels up finds the real interior node",
      api3.get("3", {}).get("inputs", {}).get("cond") == ["2:7:5", 0], str(api3.get("3")))
check("nesting reports no warnings", warns3 == [], str(warns3))

# --------------------------------------------------------------------------- #
# 4. A widget promoted onto the instance must reach the interior input
# --------------------------------------------------------------------------- #
inst = instance(2, "SG1", widgets=["promoted prompt"])
inst["inputs"] = [{"name": "clip", "type": "CLIP", "link": 1}]  # text is a widget, not a wire
wf4 = {
    "nodes": [LOADER, inst, CONSUMER],
    "links": [[1, 1, 1, 2, 0, "CLIP"], [2, 2, 0, 3, 0, "CONDITIONING"]],
    "definitions": {"subgraphs": [encode_def(promoted=True)]},
}
api4, warns4 = litegraph_to_api(wf4, OBJECT_INFO)
check("promoted widget value lands on the interior input",
      api4.get("2:5", {}).get("inputs", {}).get("text") == "promoted prompt", str(api4.get("2:5")))
check("a promoted widget is not also reported as a lost connection",
      not any("lost its connection" in w for w in warns4), str(warns4))

# --------------------------------------------------------------------------- #
# 5. Interior links serialized as objects, not arrays
# --------------------------------------------------------------------------- #
obj_def = encode_def("SG_OBJ")
obj_def["links"] = [
    {"id": 11, "origin_id": -10, "origin_slot": 0, "target_id": 5, "target_slot": 0, "type": "CLIP"},
    {"id": 12, "origin_id": 5, "origin_slot": 0, "target_id": -20, "target_slot": 0,
     "type": "CONDITIONING"},
]
wf5 = {
    "nodes": [LOADER, instance(2, "SG_OBJ"), CONSUMER],
    "links": [[1, 1, 1, 2, 0, "CLIP"], [2, 2, 0, 3, 0, "CONDITIONING"]],
    "definitions": {"subgraphs": [obj_def]},
}
api5, _ = litegraph_to_api(wf5, OBJECT_INFO)
check("object-form interior links are read, not dropped",
      api5.get("2:5", {}).get("inputs", {}).get("clip") == ["1", 1], str(api5.get("2:5")))

# --------------------------------------------------------------------------- #
# 6. A definition that contains itself must warn, not hang
# --------------------------------------------------------------------------- #
self_def = encode_def("SG_SELF")
self_def["nodes"].append({
    "id": 9, "type": "SG_SELF", "inputs": [{"name": "clip", "type": "CLIP", "link": None}],
    "outputs": [], "widgets_values": [],
})
wf6 = {
    "nodes": [LOADER, instance(2, "SG_SELF"), CONSUMER],
    "links": [[1, 1, 1, 2, 0, "CLIP"], [2, 2, 0, 3, 0, "CONDITIONING"]],
    "definitions": {"subgraphs": [self_def]},
}
_, _, warns6 = flatten(wf6)
check("self-referential definition terminates with a warning",
      any("defines itself" in w for w in warns6), str(warns6))

# --------------------------------------------------------------------------- #
# 7. A graph with no subgraphs must be untouched
# --------------------------------------------------------------------------- #
plain = {"nodes": [LOADER], "links": []}
flat, literals, warns7 = flatten(plain)
check("a graph without subgraphs passes through unchanged",
      flat is plain and literals == {} and warns7 == [])

# --------------------------------------------------------------------------- #
# 8. A wired widget input still occupies its widgets_values slot
#
# Promoting a widget onto a subgraph is exactly the "convert widget to input"
# move, and litegraph keeps the value in place. Consume it or every later widget
# shifts by one — silently, on a graph that still runs.
# --------------------------------------------------------------------------- #
LATENT_OI = {
    "EmptySD3LatentImage": {
        "input": {"required": {"width": ["INT"], "height": ["INT"], "batch_size": ["INT"]}}
    },
    "IntSource": {"input": {"required": {}}, "output": ["INT"]},
}
wf8 = {
    "nodes": [
        {"id": 1, "type": "IntSource", "inputs": [], "outputs": [{"name": "INT", "links": [1, 2]}],
         "widgets_values": []},
        {"id": 2, "type": "EmptySD3LatentImage", "widgets_values": [1024, 1024, 1],
         "inputs": [
             {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": 1},
             {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": 2},
         ], "outputs": []},
    ],
    "links": [[1, 1, 0, 2, 0, "INT"], [2, 1, 0, 2, 1, "INT"]],
}
api8, _ = litegraph_to_api(wf8, LATENT_OI)
check("wired widget inputs keep their wires",
      api8["2"]["inputs"]["width"] == ["1", 0] and api8["2"]["inputs"]["height"] == ["1", 0])
check("the widget AFTER two wired ones gets its own value, not theirs",
      api8["2"]["inputs"]["batch_size"] == 1, str(api8["2"]["inputs"]))

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all subgraph tests passed")
