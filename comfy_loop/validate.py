# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Pre-flight a graph against the live install, before spending a GPU minute on it.

`/prompt` already validates — that is what `node_errors` is — but it validates by
QUEUEING, one failure at a time, and a model that submits to find out what is
wrong burns a round trip per mistake. Worse, the failures it reports first are
rarely the ones that matter: a missing checkpoint and a missing node pack are the
same red box, though one is a download and the other needs a restart.

So this reads object_info and answers the whole question at once, sorted by what
the caller has to DO about it:

  install a node pack   the class does not exist here
  fetch a model         the file is not in that loader's list
  fix the graph         a required input is missing, a wire points at nothing
  look again            a value is outside the node's declared range

Pure logic — dict in, dict out, no HTTP — so it is testable offline and the same
function serves a graph you built, a template you fetched, and a run you are
about to repeat.
"""

from __future__ import annotations

import difflib
from typing import Any

# What ComfyUI resolves without a backend class of its own.
_VIRTUAL = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode", "Reroute (rgthree)"}


def _enum_values(spec: Any) -> list[str] | None:
    """The allowed values of a combo input, in either encoding object_info uses."""
    if not isinstance(spec, list) or not spec:
        return None
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        opts = spec[1].get("options")
        return [str(x) for x in opts] if isinstance(opts, list) else None
    if isinstance(spec[0], list):
        return [str(x) for x in spec[0]]
    return None


def _is_link(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
    )


def _nearest_fix(value: str, enum: list[str], cls: str, name: str) -> str:
    """Always hand back something to try.

    "not in the list" is a dead end; "you have sd15.safetensors" is a fix. Fuzzy
    match first, then a stem match — `sd15.ckpt` vs `sd15.safetensors` is the same
    weights in a different container and scores below any sane fuzzy cutoff — and
    failing both, name what this box actually has.
    """
    near = difflib.get_close_matches(value, enum, n=3, cutoff=0.4)
    if not near:
        stem = value.rsplit(".", 1)[0].lower()
        near = [e for e in enum if stem and stem in e.lower()][:3]
    if near:
        return f"closest already installed: {', '.join(near)}"
    if enum:
        head = ", ".join(enum[:5]) + ("…" if len(enum) > 5 else "")
        return (f"this install offers {len(enum)}: {head} — or search_models + "
                f"install_model to fetch '{value}'")
    return (f"this install offers NONE for {cls}.{name} — nothing is downloaded yet; "
            "search_models + install_model first")


def validate(graph: dict, object_info: dict) -> dict:
    """Check an API/prompt-format graph against object_info.

    Returns {"blockers": [...], "warnings": [...], "missing_classes": [...],
    "missing_files": [...], "output_nodes": int} where each finding is
    {"node", "class", "input", "problem", "fix"}.
    """
    blockers: list[dict] = []
    warnings: list[dict] = []
    missing_classes: list[str] = []
    missing_files: list[dict] = []
    output_nodes = 0

    if not isinstance(graph, dict) or not graph:
        return {
            "blockers": [{"node": "-", "class": "-", "input": "-",
                          "problem": "not an API-format graph",
                          "fix": "pass {node_id: {class_type, inputs}} — litegraph goes "
                                 "through flowzip_to_api first"}],
            "warnings": [], "missing_classes": [], "missing_files": [], "output_nodes": 0,
        }

    for nid, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node:
            blockers.append({"node": str(nid), "class": "-", "input": "-",
                             "problem": "node is not {class_type, inputs}",
                             "fix": "this is litegraph, not API format — convert with flowzip_to_api"})
            continue
        cls = node.get("class_type")
        inputs = node.get("inputs") or {}
        spec = object_info.get(cls)
        if spec is None:
            if cls in _VIRTUAL:
                continue
            missing_classes.append(cls)
            near = difflib.get_close_matches(str(cls), list(object_info), n=3, cutoff=0.7)
            blockers.append({
                "node": str(nid), "class": str(cls), "input": "-",
                "problem": "node class is not installed here",
                "fix": (f"did you mean {', '.join(near)}?" if near else
                        "find_missing_nodes names the pack, then install_node_pack + restart_comfyui"),
            })
            continue
        if spec.get("output_node"):
            output_nodes += 1

        fields = spec.get("input", {}) or {}
        required = fields.get("required") or {}
        optional = fields.get("optional") or {}
        known = {**required, **optional}

        for name in required:
            if name not in inputs:
                blockers.append({"node": str(nid), "class": str(cls), "input": name,
                                 "problem": "required input is missing",
                                 "fix": f"get_node('{cls}') for its exact interface"})

        for name, value in inputs.items():
            if _is_link(value):
                src = str(value[0])
                if src not in {str(k) for k in graph}:
                    blockers.append({"node": str(nid), "class": str(cls), "input": name,
                                     "problem": f"wired to node '{src}', which is not in the graph",
                                     "fix": "re-point the wire, or add the missing node"})
                continue
            ispec = known.get(name)
            if ispec is None:
                if name not in known:
                    warnings.append({"node": str(nid), "class": str(cls), "input": name,
                                     "problem": "input is not on this node's interface",
                                     "fix": f"get_node('{cls}') — the node may have changed version"})
                continue
            enum = _enum_values(ispec)
            if enum is not None:
                if str(value) not in enum:
                    missing_files.append({"node": str(nid), "class": str(cls),
                                          "input": name, "value": str(value)})
                    blockers.append({
                        "node": str(nid), "class": str(cls), "input": name,
                        "problem": f"'{value}' is not one of the {len(enum)} values this install offers",
                        "fix": _nearest_fix(str(value), enum, cls, name),
                    })
                continue
            opts = ispec[1] if isinstance(ispec, list) and len(ispec) > 1 and isinstance(ispec[1], dict) else {}
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lo, hi = opts.get("min"), opts.get("max")
                if isinstance(lo, (int, float)) and value < lo:
                    warnings.append({"node": str(nid), "class": str(cls), "input": name,
                                     "problem": f"{value} is below the declared min {lo}",
                                     "fix": "clamp it, or the node may reject the run"})
                elif isinstance(hi, (int, float)) and value > hi:
                    warnings.append({"node": str(nid), "class": str(cls), "input": name,
                                     "problem": f"{value} is above the declared max {hi}",
                                     "fix": "clamp it, or the node may reject the run"})

    if output_nodes == 0 and graph:
        warnings.append({"node": "-", "class": "-", "input": "-",
                         "problem": "no output node — this graph produces no file to look at",
                         "fix": "add SaveImage / PreviewImage (or the video equivalent), "
                                "or the loop has nothing to judge"})

    return {"blockers": blockers, "warnings": warnings,
            "missing_classes": sorted(set(missing_classes)),
            "missing_files": missing_files, "output_nodes": output_nodes}
