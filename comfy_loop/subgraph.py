# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Flatten litegraph subgraphs so a graph that uses them can actually RUN.

A subgraph is a frontend-only abstraction: the canvas shows one tidy node, but
`/prompt` has never heard of it. The saved workflow carries a definition under
`definitions.subgraphs` and, on the canvas, an *instance* node whose `type` is
the definition's UUID. Nothing in the backend expands that — so a converter that
doesn't either has to skip the instance, and skipping it doesn't produce a
smaller result, it produces a graph with a hole in the middle of the pipe.

Half the modern catalog is authored this way now, so "skipped: <uuid> (subgraph)"
is the difference between a template running and not running at all.

What this does: replace every instance with the definition's interior nodes,
namespaced `<instance-id>:<inner-id>` so two instances of the same definition
can't collide, and rewire the wires that crossed the boundary:

    outer producer ──▶ [instance input k] ══▶ inner consumer
    inner producer ══▶ [instance output k] ──▶ outer consumer

Both boundaries vanish; the surviving link goes straight from the real producer
to the real consumer. Nesting recurses, and a widget promoted onto the instance
comes back as a literal on the interior input it was promoted from.

Everything here is pure dict-in/dict-out — no HTTP, no object_info — so it is
testable offline (tests/test_subgraph.py).
"""

from __future__ import annotations

from typing import Any

# litegraph reserves two node ids inside a definition for the boundary itself:
# the "input node" every interior wire starts from, and the "output node" they
# end at. Serializations differ on whether they spell them out, so the ids are
# read from the definition when present and fall back to the reserved values.
SUBGRAPH_INPUT_ID = -10
SUBGRAPH_OUTPUT_ID = -20

_MAX_DEPTH = 8  # a definition that contains itself would otherwise recurse forever


def _norm_link(lk: Any) -> tuple | None:
    """(id, origin_id, origin_slot, target_id, target_slot, type) from either form.

    Root-level links are arrays; subgraph interiors have been seen serialized as
    objects. Reading only one shape silently loses every interior wire — the
    nodes arrive, unconnected, and the run fails somewhere else entirely.
    """
    if isinstance(lk, dict):
        if lk.get("id") is None:
            return None
        return (
            lk.get("id"), lk.get("origin_id"), lk.get("origin_slot"),
            lk.get("target_id"), lk.get("target_slot"), lk.get("type"),
        )
    if isinstance(lk, (list, tuple)) and len(lk) >= 5:
        return (lk[0], lk[1], lk[2], lk[3], lk[4], lk[5] if len(lk) > 5 else None)
    return None


def collect_definitions(wf: dict) -> dict[str, dict]:
    """Every subgraph definition reachable from a workflow, keyed by id.

    ComfyUI keeps them flat at the root, but a hand-assembled or older file can
    nest them inside a definition, so both are gathered.
    """
    out: dict[str, dict] = {}

    def walk(container: Any) -> None:
        if not isinstance(container, dict):
            return
        for sg in (container.get("definitions") or {}).get("subgraphs") or []:
            sid = sg.get("id")
            if isinstance(sid, str) and sid not in out:
                out[sid] = sg
                walk(sg)

    walk(wf)
    return out


def _boundary_ids(sg: dict | None) -> tuple[Any, Any]:
    if not sg:
        return SUBGRAPH_INPUT_ID, SUBGRAPH_OUTPUT_ID
    in_id = (sg.get("inputNode") or {}).get("id", SUBGRAPH_INPUT_ID)
    out_id = (sg.get("outputNode") or {}).get("id", SUBGRAPH_OUTPUT_ID)
    return in_id, out_id


def _def_output_slot(instance: dict, sg: dict, inst_slot: Any) -> Any:
    """Map an instance's output slot onto the definition's, by name."""
    inst_names = _slot_names(instance.get("outputs"))
    def_names = _slot_names(sg.get("outputs"))
    if isinstance(inst_slot, int) and 0 <= inst_slot < len(inst_names):
        name = inst_names[inst_slot]
        if name in def_names:
            return def_names.index(name)
    return inst_slot


class _Scope:
    """One level of the graph: the root, or one expanded instance.

    The boundary is addressed by slot NAME, never by position. An instance lists
    only the slots it draws as sockets, in its own order, while interior wires
    index the DEFINITION's slot list — so position-matching quietly attributes a
    wire to the wrong input, which produces a graph that runs and is wrong.
    """

    __slots__ = ("sg", "prefix", "nodes", "links", "out_link", "parent",
                 "inst_link", "promoted", "path", "in_id", "out_id")

    def __init__(self, sg, prefix, nodes, links, parent, inst_link, promoted, path):
        self.sg = sg
        self.prefix = prefix
        self.nodes = nodes            # original id -> node dict
        self.links = links            # link id -> normalized tuple
        self.parent = parent          # _Scope | None
        self.inst_link = inst_link    # definition input slot -> link id IN THE PARENT
        self.promoted = promoted      # definition input slot -> value typed on the instance
        self.path = path              # definition ids on the way here (cycle guard)
        self.in_id, self.out_id = _boundary_ids(sg)
        self.out_link = {lk[4]: lk[0] for lk in links.values() if lk[3] == self.out_id}


def _scope(sg, prefix, nodes_list, links_list, parent, inst_link, promoted, path) -> _Scope:
    nodes = {n.get("id"): n for n in (nodes_list or []) if isinstance(n, dict)}
    links: dict[Any, tuple] = {}
    for raw in links_list or []:
        lk = _norm_link(raw)
        if lk is not None:
            links[lk[0]] = lk
    return _Scope(sg, prefix, nodes, links, parent, inst_link, promoted, path)


def _slot_names(entries: Any) -> list:
    return [e.get("name") for e in entries or [] if isinstance(e, dict)]


def _boundary_wiring(scope: _Scope, node: dict, sg: dict) -> tuple[dict, dict]:
    """How the parent feeds one instance: (slot -> parent link, slot -> value).

    Two facts, learned from the shipped catalog rather than assumed:
      * a wire is matched by slot NAME, because the instance's socket list is a
        subset of the definition's, in its own order;
      * `widgets_values` runs positionally over the definition's slots but SKIPS
        the wired ones — video_ltx2_i2v carries 7 values for 8 slots, the missing
        one being the single slot the parent wires.
    A slot with neither wire nor value is not a hole: the interior node still
    holds the value the widget was promoted from.
    """
    names = _slot_names(sg.get("inputs"))
    wired_by_name = {
        i.get("name"): i.get("link")
        for i in node.get("inputs") or []
        if isinstance(i, dict) and i.get("link") is not None
    }
    inst_link = {k: wired_by_name[n] for k, n in enumerate(names) if n in wired_by_name}
    values = list(node.get("widgets_values") or [])
    promoted: dict[int, Any] = {}
    vi = 0
    for k in range(len(names)):
        if k in inst_link or vi >= len(values):
            continue
        promoted[k] = values[vi]
        vi += 1
    return inst_link, promoted


def flatten(wf: dict) -> tuple[dict, dict[str, dict[str, Any]], list[str]]:
    """Expand every subgraph instance.

    Returns (flat_workflow, literals, warnings):
      flat_workflow  litegraph-shaped, instances replaced by their interiors,
                     link ids renumbered, node ids namespaced. Reroutes are left
                     alone — resolving those is litegraph_to_api's job.
      literals       {node_id: {input_name: value}} for widgets promoted onto an
                     instance; applied after the positional widget mapping.
      warnings       anything that could not be resolved, in caller-facing words.
    """
    defs = collect_definitions(wf)
    if not defs:
        return wf, {}, []

    warnings: list[str] = []
    emitted: list[tuple[_Scope, dict, str]] = []   # (scope, original node, new id)
    literals: dict[str, dict[str, Any]] = {}
    child_scopes: dict[tuple[int, Any], _Scope] = {}

    root = _scope(None, "", wf.get("nodes"), wf.get("links"), None, {}, [], ())

    def expand(scope: _Scope, depth: int) -> None:
        for nid, node in scope.nodes.items():
            ntype = node.get("type")
            if ntype in defs:
                sg = defs[ntype]
                new_prefix = f"{scope.prefix}{nid}:"
                if depth >= _MAX_DEPTH or ntype in scope.path:
                    warnings.append(
                        f"subgraph {sg.get('name') or ntype} at {new_prefix} not expanded "
                        f"({'nested too deep' if depth >= _MAX_DEPTH else 'defines itself'})"
                    )
                    continue
                inst_link, promoted = _boundary_wiring(scope, node, sg)
                child = _scope(
                    sg, new_prefix, sg.get("nodes"), sg.get("links"), scope,
                    inst_link, promoted, scope.path + (ntype,),
                )
                child_scopes[(id(scope), nid)] = child
                expand(child, depth + 1)
            else:
                emitted.append((scope, node, f"{scope.prefix}{nid}"))

    expand(root, 0)

    def resolve(scope: _Scope, link_id: Any, depth: int = 0) -> tuple[str, int] | None:
        """Follow a wire back to a node that actually exists in the flat graph."""
        if depth > _MAX_DEPTH * 2:
            return None
        lk = scope.links.get(link_id)
        if lk is None:
            return None
        _, oid, oslot, _, _, _ = lk
        if oid == scope.in_id and scope.parent is not None:
            parent_link = scope.inst_link.get(oslot)
            if parent_link is None:
                return None  # boundary fed by a promoted widget, not a wire
            return resolve(scope.parent, parent_link, depth + 1)
        node = scope.nodes.get(oid)
        if node is None:
            return None
        if node.get("type") in defs:
            child = child_scopes.get((id(scope), oid))
            if child is None:
                return None
            inner = child.out_link.get(_def_output_slot(node, defs[node["type"]], oslot))
            if inner is None:
                return None
            return resolve(child, inner, depth + 1)
        return f"{scope.prefix}{oid}", oslot

    # Promoted widgets: a value typed on the instance belongs to whatever interior
    # input the boundary slot feeds. Only slots the parent left unwired can carry
    # one, and they consume widgets_values in slot order.
    def apply_promoted(scope: _Scope) -> None:
        for slot_idx, value in scope.promoted.items():
            for lk in scope.links.values():
                if lk[1] != scope.in_id or lk[2] != slot_idx:
                    continue
                target = scope.nodes.get(lk[3])
                if not target:
                    continue
                tin = target.get("inputs") or []
                if lk[4] is None or lk[4] >= len(tin):
                    continue
                name = tin[lk[4]].get("name")
                if name:
                    literals.setdefault(f"{scope.prefix}{lk[3]}", {})[name] = value

    for scope in child_scopes.values():
        apply_promoted(scope)

    # Rebuild: every surviving node keeps its inputs, but each wire is re-pointed
    # at the real producer and given a fresh id (interior link ids collide across
    # scopes, and a collision would silently rewire the graph).
    flat_nodes: list[dict] = []
    flat_links: list[list] = []
    next_link = 1
    for scope, node, new_id in emitted:
        out = dict(node)
        out["id"] = new_id
        out["outputs"] = [
            {**o, "links": []} for o in (node.get("outputs") or []) if isinstance(o, dict)
        ]
        new_inputs = []
        for idx, inp in enumerate(node.get("inputs") or []):
            copy = dict(inp)
            link_id = inp.get("link")
            if link_id is None:
                copy["link"] = None
            else:
                src = resolve(scope, link_id)
                if src is None:
                    copy["link"] = None
                    # An unwired boundary slot is the NORMAL case for a promoted
                    # widget: the interior node still holds the value, and the
                    # positional widget mapping downstream will find it. Only a
                    # real data socket losing its producer is worth a warning.
                    promoted_here = (literals.get(new_id) or {}).get(inp.get("name")) is not None
                    if not promoted_here and "widget" not in inp:
                        warnings.append(
                            f"{node.get('type')}.{inp.get('name')} at {new_id} lost its "
                            "connection crossing a subgraph boundary — wire it or set it by hand"
                        )
                else:
                    prod, slot = src
                    flat_links.append([next_link, prod, slot, new_id, idx, inp.get("type")])
                    copy["link"] = next_link
                    next_link += 1
            new_inputs.append(copy)
        out["inputs"] = new_inputs
        flat_nodes.append(out)

    flat = {k: v for k, v in wf.items() if k not in {"nodes", "links", "definitions"}}
    flat["nodes"] = flat_nodes
    flat["links"] = flat_links
    return flat, literals, warnings
