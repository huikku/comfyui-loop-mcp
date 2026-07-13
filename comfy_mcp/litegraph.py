"""API/prompt format -> UI/litegraph format, the inverse of compress.litegraph_to_api.

The loop builds and validates in API format because that's what /prompt runs. But
an artist wants the file they can drag onto the canvas, and that's a different
shape: link objects with ids, positional widgets_values, node positions. Without
this, "I'll give you the UI version if you ask" is a promise the server can't keep.

The dangerous part is widgets_values: it is POSITIONAL, in object_info's input
order, excludes wired inputs, and seed-ish widgets carry an extra
control_after_generate value. Get it wrong and the graph opens with silently
shifted parameters — worse than failing. So we build it straight from object_info,
and then prove it by running the result back through litegraph_to_api and
diffing: if the round-trip doesn't reproduce the API graph we started from, we
say so instead of handing over a plausible lie.
"""

from __future__ import annotations

from typing import Any

from .compress import _is_widget_type, litegraph_to_api

_SEED_NAMES = {"seed", "noise_seed"}


def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[1], int)


def _depths(api: dict) -> dict[str, int]:
    """Longest-path depth per node, for a left-to-right layout that reads like a graph."""
    memo: dict[str, int] = {}

    def depth(nid: str, seen: frozenset[str] = frozenset()) -> int:
        if nid in memo:
            return memo[nid]
        if nid in seen:  # a cycle shouldn't exist, but never hang on one
            return 0
        node = api.get(nid) or {}
        parents = [str(v[0]) for v in (node.get("inputs") or {}).values() if _is_link(v)]
        d = 0 if not parents else 1 + max(depth(p, seen | {nid}) for p in parents)
        memo[nid] = d
        return d

    return {nid: depth(nid) for nid in api}


def api_to_litegraph(api: dict, object_info: dict) -> tuple[dict, list[str]]:
    """Build a UI-loadable litegraph workflow from an API graph.

    Returns (workflow, warnings). Warnings include any round-trip mismatch — treat
    a non-empty mismatch as "do not ship this file."
    """
    warnings: list[str] = []
    depths = _depths(api)
    by_depth: dict[int, list[str]] = {}
    for nid, d in sorted(depths.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        by_depth.setdefault(d, []).append(nid)

    # Lay out columns by dependency depth, rows within a column.
    pos: dict[str, list[int]] = {}
    for d, ids in by_depth.items():
        for row, nid in enumerate(ids):
            pos[nid] = [d * 340, row * 200]

    nodes: list[dict] = []
    links: list[list] = []
    link_id = 0
    # output slot index per (source node, output type) — resolved from object_info
    out_links: dict[str, dict[int, list[int]]] = {nid: {} for nid in api}
    node_inputs: dict[str, list[dict]] = {nid: [] for nid in api}

    # First pass: create links, recording both endpoints.
    for nid, node in api.items():
        cls = node.get("class_type")
        spec = object_info.get(cls, {})
        in_spec = spec.get("input", {})
        ordered = list((in_spec.get("required") or {}).items()) + list(
            (in_spec.get("optional") or {}).items()
        )
        by_name = dict(ordered)
        for iname, val in (node.get("inputs") or {}).items():
            if not _is_link(val):
                continue
            src_id, src_slot = str(val[0]), int(val[1])
            if src_id not in api:
                warnings.append(f"node {nid}.{iname} links to missing node {src_id}")
                continue
            src_cls = api[src_id].get("class_type")
            src_outputs = object_info.get(src_cls, {}).get("output", []) or []
            ltype = src_outputs[src_slot] if src_slot < len(src_outputs) else "*"
            if isinstance(ltype, list):  # a COMBO output surfaces as a list
                ltype = "COMBO"
            ispec = by_name.get(iname)
            itype = ispec[0] if isinstance(ispec, list) and ispec else "*"
            if isinstance(itype, list):
                itype = "COMBO"
            link_id += 1
            links.append([link_id, int(src_id), src_slot, int(nid), len(node_inputs[nid]), ltype])
            node_inputs[nid].append({"name": iname, "type": itype, "link": link_id})
            out_links.setdefault(src_id, {}).setdefault(src_slot, []).append(link_id)

    # Second pass: build the nodes, with positional widgets_values.
    for nid, node in api.items():
        cls = node.get("class_type")
        spec = object_info.get(cls)
        if spec is None:
            warnings.append(f"{cls} not in object_info — node emitted without widgets")
            spec = {}
        in_spec = spec.get("input", {})
        ordered = list((in_spec.get("required") or {}).items()) + list(
            (in_spec.get("optional") or {}).items()
        )
        inputs = node.get("inputs") or {}

        widgets: list[Any] = []
        for iname, ispec in ordered:
            val = inputs.get(iname)
            if _is_link(val):
                continue  # wired -> a slot, never a widget value
            itype = ispec[0] if isinstance(ispec, list) and ispec else ispec
            if not _is_widget_type(itype):
                continue
            if iname not in inputs:
                # Unset optional widget: litegraph still reserves its slot.
                opts = ispec[1] if isinstance(ispec, list) and len(ispec) > 1 and isinstance(ispec[1], dict) else {}
                widgets.append(opts.get("default", ""))
            else:
                widgets.append(val)
            opts = ispec[1] if isinstance(ispec, list) and len(ispec) > 1 and isinstance(ispec[1], dict) else {}
            if opts.get("control_after_generate") or iname in _SEED_NAMES:
                widgets.append("fixed")  # the extra value litegraph stores after a seed

        outs = spec.get("output", []) or []
        out_names = spec.get("output_name", []) or []
        outputs = []
        for i, otype in enumerate(outs):
            t = "COMBO" if isinstance(otype, list) else otype
            outputs.append(
                {
                    "name": (out_names[i] if i < len(out_names) else t),
                    "type": t,
                    "slot_index": i,
                    "links": out_links.get(nid, {}).get(i, []),
                }
            )

        nodes.append(
            {
                "id": int(nid) if str(nid).isdigit() else nid,
                "type": cls,
                "pos": pos.get(nid, [0, 0]),
                "size": [270, 100 + 26 * max(len(widgets), 1)],
                "flags": {},
                "order": depths.get(nid, 0),
                "mode": 0,
                "inputs": node_inputs.get(nid, []),
                "outputs": outputs,
                "properties": {"Node name for S&R": cls},
                "widgets_values": widgets,
            }
        )

    wf = {
        "id": "",
        "revision": 0,
        "last_node_id": max((int(n) for n in api if str(n).isdigit()), default=0),
        "last_link_id": link_id,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }

    # Prove it: the file must convert BACK to the API graph we were given.
    try:
        back, _ = litegraph_to_api(wf, object_info)
        for nid, node in api.items():
            got = back.get(nid)
            if got is None:
                warnings.append(f"round-trip lost node {nid} ({node.get('class_type')})")
                continue
            for iname, val in (node.get("inputs") or {}).items():
                if iname not in got.get("inputs", {}):
                    warnings.append(f"round-trip lost {nid}.{iname}")
                elif got["inputs"][iname] != val:
                    warnings.append(
                        f"round-trip changed {nid}.{iname}: {val!r} -> {got['inputs'][iname]!r}"
                    )
    except Exception as e:  # a converter bug must not masquerade as a good file
        warnings.append(f"round-trip check failed to run: {e}")

    return wf, warnings
