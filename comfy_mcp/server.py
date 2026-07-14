"""
comfy-mcp — a loop-aware MCP server for ComfyUI.

It wraps the ComfyUI REST API as MCP **tools**, exposes the build→run→look→
critique→fix methodology as MCP **prompts**, and serves the live node truth +
the onboarding docs as MCP **resources**.

The point isn't just "call the API." Every tool description and response nudges
the model through the loop: discover before building, validate by executing,
and — the step models skip — *actually look at the pixels* before deciding a
graph is done. A graph with zero node_errors is valid, not correct.

Config via env:
  COMFYUI_URL             base URL of the ComfyUI server (default http://localhost:8188)
  COMFYUI_ONBOARDING_DIR  dir holding the loop/skill markdown (default: repo root above this pkg)
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP, Image

from . import imaging
from . import loop as loopstate
from . import report
from .compress import (
    _NODE_LEGEND,
    compact_node,
    flowzip_deflate,
    flowzip_inflate,
    litegraph_to_api,
)
from .litegraph import api_to_litegraph

COMFY_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188").rstrip("/")

# The official, open template catalog — the same repo the Cloud MCP's template
# search is built from. Lets us browse/fetch all ~550 templates WITHOUT installing
# them, straight from GitHub. Override the ref with COMFYUI_TEMPLATES_REF.
_TPL_REPO = "https://raw.githubusercontent.com/Comfy-Org/workflow_templates"
_TPL_REF = os.environ.get("COMFYUI_TEMPLATES_REF", "main")
_TPL_BASE = f"{_TPL_REPO}/{_TPL_REF}"

# The loop/skill docs ship INSIDE the package, so an installed server is
# self-contained — a `pip install` from anywhere still serves the prompts. They're
# vendored from huikku/comfyui-llm-onboarding-prompt (same author, MIT); point
# COMFYUI_ONBOARDING_DIR at a checkout of that repo to serve them live instead.
_PKG_DOCS = Path(__file__).resolve().parent / "docs"
_DOCS_DIR = (
    Path(os.environ["COMFYUI_ONBOARDING_DIR"])
    if os.environ.get("COMFYUI_ONBOARDING_DIR")
    else _PKG_DOCS
)

mcp = FastMCP(
    "comfyui",
    instructions=(
        "Tools + prompts for building ComfyUI workflows the reliable way.\n\n"
        "ALWAYS: discover real nodes/models from the live API (list_nodes / "
        "get_node / list_models) before writing JSON — never guess a node name, "
        "input name, type, or model filename. Build API/prompt format. Validate "
        "by executing (submit_workflow); node_errors are not iterations — read "
        "them, fix that node, re-submit.\n\n"
        "PREFER LOOPING when the goal is a good *result*, not just a graph that "
        "runs — i.e. whenever a trained eye could reject the output: composition/"
        "count, likeness, matte/edge quality, upscale/restore, relight, texture "
        "seams, video temporal stability, 'make it look right'. Then run the loop: "
        "loop_start once, then each pass: get_result -> get_image to actually LOOK "
        "(compare_images against your best-so-far — 'difference' mode makes drift you'd "
        "never see by eye pop), name ONE concrete defect, change ONE parameter, re-run, "
        "and loop_record it. Repeat until you cannot name a real defect, then loop_finish "
        "and present the ledger for sign-off. The RATCHET is a TOOL, not a memory "
        "exercise: loop_record stores the best graph server-side and hands it back on a "
        "regression, so REVERT is one call and the best-so-far survives context "
        "compaction — never build on a regression, and never trust your recollection of "
        "which pass was best over loop_best. Pivot param -> wiring -> model on plateau. A "
        "graph with zero node_errors is VALID, NOT CORRECT — never trust a green run or a "
        "single metric; where the brief has an objective gate (seamless tile, sharpness) "
        "score it with measure_image and pass the score to loop_record, judge by eye "
        "otherwise. Load the `comfy_loop` prompt for the full autonomous method.\n\n"
        "SKIP the loop only for mechanical, non-aesthetic tasks (a format "
        "conversion, a one-shot where the user explicitly wants just a runnable "
        "graph, or a pure API query). When unsure, do at least one look-and-"
        "critique pass before declaring done."
    ),
)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=COMFY_URL, timeout=30.0)


def _read_doc(name: str) -> str:
    # Prefer the override dir, but always fall back to the packaged copy — a bad
    # COMFYUI_ONBOARDING_DIR must not leave the prompts empty.
    for d in (_DOCS_DIR, _PKG_DOCS):
        path = d / name
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                break
    path = _DOCS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return f"(could not read {name} from {_DOCS_DIR} — set COMFYUI_ONBOARDING_DIR)"


def _extract_enum(spec: Any) -> list[str] | None:
    """A loader input's valid files live in one of two encodings.

    Legacy: type is a list → element 0 is the allowed values.
    Newer:  type is "COMBO" → element 1 is {"options": [...]}.
    """
    if not isinstance(spec, list) or not spec:
        return None
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        opts = spec[1].get("options")
        return list(opts) if isinstance(opts, list) else None
    if isinstance(spec[0], list):
        return [str(x) for x in spec[0]]
    return None


# --------------------------------------------------------------------------- #
# DISCOVER
# --------------------------------------------------------------------------- #
@mcp.tool()
async def check_comfyui() -> str:
    """Confirm ComfyUI is up and reachable (loop step 0).

    Returns the installed-node count and device/VRAM. If this fails, ComfyUI
    isn't running or is on another port — do not start guessing node names.
    """
    try:
        async with _client() as c:
            info = (await c.get("/object_info")).json()
            stats = (await c.get("/system_stats")).json()
    except Exception as e:  # noqa: BLE001
        return (
            f"ComfyUI is NOT reachable at {COMFY_URL} ({e}). "
            "Check the host/port, or start ComfyUI. Do not generate workflow "
            "JSON until the API answers — you'd only be guessing node names."
        )
    devices = ", ".join(
        f"{d.get('name')} {round(d.get('vram_total', 0) / 1e9, 1)}GB"
        for d in stats.get("devices", [])
    )
    return f"ComfyUI up at {COMFY_URL}: {len(info)} nodes installed. Devices: {devices or 'n/a'}."


@mcp.tool()
async def list_nodes(keyword: str = "") -> str:
    """Search installed nodes by keyword (loop step: discover, don't guess).

    Matches the keyword against BOTH the class name and the display name, so a
    node found by its UI label ("Load Image") still turns up. Returns
    class_name -> display_name. Class names are case-sensitive and differ from
    display names — use the class_name in workflow JSON. Omit keyword to get the
    total count only (the full list is large).
    """
    async with _client() as c:
        info: dict[str, Any] = (await c.get("/object_info")).json()
    kw = keyword.lower().strip()
    if not kw:
        return f"{len(info)} nodes installed. Pass a keyword to filter, or use get_node for one node's exact interface."
    hits = {
        k: info[k].get("display_name", k)
        for k in sorted(info)
        if kw in k.lower() or kw in str(info[k].get("display_name", "")).lower()
    }
    if not hits:
        return f"No node matches '{keyword}' by class or display name. Try a broader keyword."
    lines = [
        f"{k}  ->  {v}" + ("   (matched display name)" if kw not in k.lower() else "")
        for k, v in hits.items()
    ]
    return f"{len(hits)} match '{keyword}' (class or display name):\n" + "\n".join(lines)


@mcp.tool()
async def get_node(class_name: str, verbose: bool = False) -> str:
    """Get one node's interface: inputs (required +, optional ?) and outputs (-).

    Default is COMPACT notation — `@Name +req:T ?opt:T -out:T` (type codes:
    {legend}) — ~90% fewer tokens than raw JSON, enough to wire the node
    correctly. Pass verbose=True for the full JSON (defaults, min/max, tooltips)
    when you need exact widget ranges.
    """.replace("{legend}", _NODE_LEGEND)
    async with _client() as c:
        r = await c.get(f"/object_info/{class_name}")
    if r.status_code != 200 or not r.json():
        return f"No node class '{class_name}'. Use list_nodes to find the correct case-sensitive class name."
    spec = r.json().get(class_name, r.json())
    if verbose:
        return json.dumps(spec, indent=2)
    return f"# {_NODE_LEGEND}\n{compact_node(class_name, spec)}"


@mcp.tool()
async def list_models(class_name: str, input_name: str = "") -> str:
    """List the real model files a loader offers (loop step: never hallucinate a
    checkpoint/LoRA/VAE filename).

    Reads the loader's enum from object_info (handles both the legacy list and
    the newer COMBO encoding). If input_name is omitted, reports every enum-typed
    input on the node. Pick ONLY from the returned list.
    """
    async with _client() as c:
        r = await c.get(f"/object_info/{class_name}")
    if r.status_code != 200 or not r.json():
        return f"No node class '{class_name}'. Use list_nodes first."
    spec = r.json()[class_name]["input"]
    fields = {**spec.get("required", {}), **spec.get("optional", {})}
    if input_name:
        enum = _extract_enum(fields.get(input_name))
        if enum is None:
            return f"'{input_name}' on {class_name} is not an enum/model input."
        return f"{class_name}.{input_name} ({len(enum)} files):\n" + "\n".join(enum)
    out = []
    for name, field_spec in fields.items():
        enum = _extract_enum(field_spec)
        if enum is not None:
            preview = enum[:50]
            more = "" if len(enum) <= 50 else f" (+{len(enum) - 50} more)"
            out.append(f"{name} ({len(enum)}):\n  " + "\n  ".join(preview) + more)
    return f"Enum inputs on {class_name}:\n\n" + "\n\n".join(out) if out else f"{class_name} has no enum inputs."


@mcp.tool()
async def search_models(keyword: str = "", model_type: str = "") -> str:
    """Search the downloadable model catalog — find checkpoints/LoRAs/VAEs/
    controlnets/upscalers you may NOT have installed yet (the local equivalent of
    the cloud's model search). Reads ComfyUI-Manager's model list; each result
    shows whether it's already installed on THIS box.

    Filter by keyword (name/filename/base/description) and optional model_type
    (checkpoint, lora, vae, controlnet, upscale, clip, diffusion_model, ...).
    Install one with install_model(name). Requires ComfyUI-Manager on the host.

    This is a catalog of *known* models; list_models shows what a specific loader
    currently offers on disk (ground truth). Discover here, then verify against
    list_models after installing.
    """
    async with _client() as c:
        r = await c.get("/externalmodel/getlist?mode=cache")
    if r.status_code != 200:
        return f"Model catalog unavailable (HTTP {r.status_code}). Needs ComfyUI-Manager on the host."
    models = r.json().get("models", [])
    kw, mt = keyword.lower().strip(), model_type.lower().strip()
    hits = []
    for m in models:
        if mt and mt not in str(m.get("type", "")).lower():
            continue
        hay = " ".join(str(m.get(k, "")) for k in ("name", "filename", "base", "description")).lower()
        if kw and kw not in hay:
            continue
        hits.append(m)
    if not hits:
        return (f"No catalog model matches (keyword={keyword!r}, type={model_type!r}) among "
                f"{len(models)}. Broaden the search, or it may not be in Manager's list.")
    lines = []
    for m in hits[:40]:
        flag = "installed" if str(m.get("installed", "")).lower() == "true" else "NOT installed"
        lines.append(f"  {m.get('name')}  [{m.get('type')}/{m.get('base', '?')}]  "
                     f"{m.get('filename')}  {m.get('size', '?')}  ({flag})")
    more = "" if len(hits) <= 40 else f"\n  … (+{len(hits) - 40} more; narrow keyword/type)"
    return (f"{len(hits)} of {len(models)} catalog models"
            + (f" matching {keyword!r}" if kw else "") + (f" type={model_type}" if mt else "")
            + ". Install with install_model(name), then verify with list_models:\n"
            + "\n".join(lines) + more)


def _flatten_index(cats: list[dict]) -> list[dict]:
    out: list[dict] = []
    for cat in cats:
        title = cat.get("title", cat.get("moduleName", ""))
        for t in cat.get("templates", []):
            out.append(
                {
                    "name": t.get("name", ""),
                    "title": t.get("title", ""),
                    "description": t.get("description", ""),
                    "category": title,
                }
            )
    return out


def _bundled_index() -> list[dict]:
    """The compressed catalog snapshot shipped with the package (fast, offline).
    Refreshed by scripts/build_template_index.py (run weekly by a GitHub Action)."""
    path = Path(__file__).parent / "data" / "templates_index.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f).get("templates", [])


_TPL_INDEX_CACHE: list[dict] | None = None


async def _online_index() -> list[dict]:
    """The template catalog, cached per process.

    Prefers the bundled compressed snapshot (instant, offline, no 566 KB fetch).
    Set COMFYUI_TEMPLATES_LIVE=1 to fetch the freshest index from GitHub instead;
    that also serves as the fallback if the snapshot is missing.
    """
    global _TPL_INDEX_CACHE
    if _TPL_INDEX_CACHE is not None:
        return _TPL_INDEX_CACHE
    if os.environ.get("COMFYUI_TEMPLATES_LIVE") != "1":
        try:
            _TPL_INDEX_CACHE = _bundled_index()
            return _TPL_INDEX_CACHE
        except Exception:  # noqa: BLE001
            pass  # snapshot missing -> fetch live
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            cats = (await c.get(f"{_TPL_BASE}/templates/index.json")).json()
        _TPL_INDEX_CACHE = _flatten_index(cats)
    except Exception:  # noqa: BLE001
        _TPL_INDEX_CACHE = _bundled_index()
    return _TPL_INDEX_CACHE


@mcp.tool()
async def search_templates(keyword: str = "", source: str = "online") -> str:
    """Search known-good workflow templates to adapt (few-shot beats zero-shot).

    source="online" (default): the OFFICIAL open catalog on GitHub
    (Comfy-Org/workflow_templates) — the same ~550-template set the Cloud MCP's
    search is built from. You do NOT need these installed; they're browsed
    straight from the repo. Matches keyword against name + title + description.

    source="installed": only templates on THIS ComfyUI right now
    (/api/workflow_templates — every installed pack's example workflows). Smaller,
    but guaranteed runnable on your install without adding anything.

    Then fetch one with get_template. Note an online template may reference nodes/
    models you haven't installed — reconcile against object_info before running.
    """
    kw = keyword.lower().strip()
    if source == "installed":
        async with _client() as c:
            idx: dict[str, list[str]] = (await c.get("/api/workflow_templates")).json()
        total = sum(len(v) for v in idx.values())
        lines: list[str] = []
        for pack in sorted(idx):
            hits = [n for n in idx[pack] if not kw or kw in n.lower() or kw in pack.lower()]
            if hits:
                lines.append(f"{pack}:")
                lines.extend(f"  {n}" for n in hits)
        if not lines:
            return f"No installed template matches '{keyword}' among {total} in {len(idx)} packs. Try source='online' for the full catalog."
        return (
            f"{total} installed templates in {len(idx)} packs"
            + (f" — matching '{keyword}'" if kw else "")
            + ". Fetch with get_template(pack, name, source='installed'):\n"
            + "\n".join(lines)
        )

    # online catalog
    try:
        entries = await _online_index()
    except Exception as e:  # noqa: BLE001
        return f"Could not reach the online template catalog ({e}). Try source='installed'."
    hits = [
        e for e in entries
        if not kw or kw in e["name"].lower() or kw in e["title"].lower() or kw in e["description"].lower()
    ]
    if not hits:
        return f"No template in the online catalog ({len(entries)} total) matches '{keyword}'. Try a broader keyword."
    lines = [f"  {e['name']}  —  {e['title']}  [{e['category']}]" for e in hits[:60]]
    more = "" if len(hits) <= 60 else f"\n  … (+{len(hits) - 60} more; narrow the keyword)"
    return (
        f"{len(hits)} of {len(entries)} online catalog templates"
        + (f" matching '{keyword}'" if kw else "")
        + ". Fetch with get_template(name=<name>, source='online'):\n"
        + "\n".join(lines) + more
    )


@mcp.tool()
async def get_template(name: str, pack: str = "", source: str = "online", fmt: str = "flowzip") -> str:
    """Fetch one workflow template as a known-good starting point.

    source="online" (default): from the official GitHub catalog — no install
    needed; `pack` is ignored. source="installed": from this ComfyUI (`pack`
    required).

    fmt="flowzip" (default): compact FlowZip text — ~72% fewer tokens than the raw
    litegraph JSON (median), enough to read/adapt the graph. fmt="json": the full litegraph.
    Either way it's litegraph, NOT the API/prompt format submit_workflow needs —
    adapt to API (resolve passthroughs, widgets_values -> named inputs via
    get_node), or inflate a FlowZip with inflate_workflow. If from the online
    catalog, first confirm you have its nodes/models — run find_missing_nodes then
    install_node_pack, or verify with list_nodes/list_models.
    """
    if source == "installed":
        if not pack:
            return "source='installed' needs a pack. Use search_templates(source='installed') for valid pack/name pairs."
        async with _client() as c:
            r = await c.get(f"/api/workflow_templates/{pack}/{name}.json")
        label = f"{pack}/{name} (installed)"
    else:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{_TPL_BASE}/templates/{name}.json")
        label = f"{name} (online catalog)"
    if r.status_code != 200:
        return f"No template '{name}' (HTTP {r.status_code}). Use search_templates to list valid names."
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return f"Template '{label}' did not return JSON."
    is_ui = isinstance(data, dict) and "nodes" in data and "links" in data
    hint = "" if source == "installed" else " (confirm nodes/models via find_missing_nodes)"
    if fmt == "flowzip" and is_ui:
        return (
            f"Template {label} — FlowZip (litegraph; inflate_workflow to expand, "
            f"then adapt to API to run){hint}\n\n{flowzip_deflate(data)}"
        )
    kind = "litegraph — adapt to API before submitting" if is_ui else "inspect before submitting"
    return f"Template {label} — {kind}{hint}\n\n" + json.dumps(data, indent=2)


@mcp.tool()
async def inflate_workflow(flowzip: str) -> str:
    """Expand FlowZip text back into a full litegraph workflow JSON.

    Reverse of the FlowZip that get_template returns. Structure (nodes, types,
    links, widget values) is preserved; cosmetic fields are not. To RUN it, still
    adapt the litegraph to API/prompt format for submit_workflow.
    """
    try:
        wf = flowzip_inflate(flowzip)
    except Exception as e:  # noqa: BLE001
        return f"Could not parse FlowZip: {e}"
    return json.dumps(wf, indent=2)


@mcp.tool()
async def flowzip_to_api(flowzip: str) -> str:
    """Convert FlowZip (or litegraph) into API/prompt format ready for submit_workflow.

    Inflates FlowZip if needed, then maps the litegraph to the flat API graph using
    the live object_info (resolves links, maps widget values to named inputs). This
    is the bridge for authoring/adapting graphs compactly and running them.

    Best-effort: subgraph instances and unknown nodes are skipped and reported;
    widget drift between an old template and a newer node shows up as a
    node_errors when you submit_workflow the result — read it, fix that node,
    re-submit. Review the API graph before running.
    """
    text = flowzip.strip()
    try:
        wf = flowzip_inflate(text) if not text.startswith("{") else json.loads(text)
    except Exception as e:  # noqa: BLE001
        return f"Could not parse input: {e}"
    async with _client() as c:
        oi = (await c.get("/object_info")).json()
    api, warnings = litegraph_to_api(wf, oi)
    note = ("\n\nSkipped (handle manually): " + "; ".join(warnings)) if warnings else ""
    return (
        f"API/prompt format ({len(api)} nodes). Review, then submit_workflow. "
        "A green run is valid, not correct — LOOK at the output." + note
        + "\n\n" + json.dumps(api, indent=2)
    )


async def _fetch_template_json(name: str, source: str, pack: str):
    """Fetch a template's raw litegraph JSON. Returns (dict|None, error|None)."""
    if source == "installed":
        if not pack:
            return None, "source='installed' needs a pack (see search_templates)."
        async with _client() as c:
            r = await c.get(f"/api/workflow_templates/{pack}/{name}.json")
    else:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{_TPL_BASE}/templates/{name}.json")
    if r.status_code != 200:
        return None, f"Template '{name}' not found (HTTP {r.status_code})."
    try:
        return r.json(), None
    except Exception:  # noqa: BLE001
        return None, f"Template '{name}' did not return JSON."


def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


@mcp.tool()
async def template_slots(name: str, source: str = "online", pack: str = "") -> str:
    """List a template's overridable inputs WITHOUT loading the full graph JSON.

    Converts the template to API format and reports each node's literal (non-wired)
    inputs and current values — the curated parameter list you can change with
    run_template. Far smaller than the raw graph. Subgraph/unknown nodes can't be
    expanded and are reported (their inputs aren't overridable this way).
    """
    wf, err = await _fetch_template_json(name, source, pack)
    if err:
        return err
    async with _client() as c:
        oi = (await c.get("/object_info")).json()
    api, warns = litegraph_to_api(wf, oi)
    lines = []
    for nid, node in api.items():
        lits = {k: v for k, v in node["inputs"].items() if not _is_link(v)}
        if lits:
            lines.append(f"  {nid} ({node['class_type']}): {json.dumps(lits, ensure_ascii=False)}")
    note = ("\nNot overridable (subgraph/unknown, skipped): " + "; ".join(warns)) if warns else ""
    return (
        f"Overridable inputs for '{name}' ({len(api)} nodes). Change them with "
        "run_template(name, overrides={node_id: {input: value}}):\n"
        + ("\n".join(lines) if lines else "  (none)")
        + note
    )


@mcp.tool()
async def run_template(name: str, overrides: dict | None = None, source: str = "online",
                       pack: str = "", client_id: str = "comfy-mcp") -> str:
    """Run a known-good template with input overrides — WITHOUT loading the graph
    into context. Fetches the template, converts to API format, applies overrides,
    and submits. Use template_slots first to see what you can override.

    overrides: {node_id: {input_name: value}} (node ids and inputs from
    template_slots). After it runs, call get_result then get_image and LOOK — a
    green run is valid, not correct.

    Limitation: subgraph templates can't be expanded (converter coverage ~88% of
    non-subgraph nodes); if nodes are skipped it's reported and the run may be
    incomplete. Confirm the template's nodes/models exist first (find_missing_nodes).
    """
    overrides = overrides or {}
    wf, err = await _fetch_template_json(name, source, pack)
    if err:
        return err
    async with _client() as c:
        oi = (await c.get("/object_info")).json()
    api, warns = litegraph_to_api(wf, oi)
    applied = []
    for nid, ins in overrides.items():
        if str(nid) in api and isinstance(ins, dict):
            api[str(nid)]["inputs"].update(ins)
            applied.append(str(nid))
        else:
            return (f"override target node '{nid}' not in the converted graph "
                    "(run template_slots to see valid node ids).")
    async with _client() as c:
        r = await c.post("/prompt", json={"prompt": api, "client_id": client_id})
    if r.status_code != 200:
        return ("REJECTED (HTTP {}). Not an iteration — read node_errors, fix, retry:\n{}"
                .format(r.status_code, r.text[:400])
                + (f"\n(skipped: {'; '.join(warns)})" if warns else ""))
    pid = r.json().get("prompt_id")
    msg = (f"Queued template '{name}' — prompt_id={pid} ({len(api)} nodes; "
           f"overrides applied to {applied or 'none'}).")
    if warns:
        msg += (f"\nWARNING: {len(warns)} node(s) skipped (subgraph/unknown) — result may be "
                f"incomplete: {'; '.join(warns[:5])}")
    msg += "\nNow: get_result(prompt_id) then get_image to LOOK, then critique and iterate."
    return msg


# --------------------------------------------------------------------------- #
# EXTEND — install what a template needs (via ComfyUI-Manager, trusted registry)
# --------------------------------------------------------------------------- #
def _node_classes(workflow: Any) -> set[str]:
    """Extract real node class names from either format.

    API format: values keyed by node id, each with class_type.
    Litegraph: top-level `nodes[].type`, PLUS nodes nested inside
    `definitions.subgraphs[].nodes`. A subgraph *instance* has type == the
    subgraph's id (a UUID) — those aren't installable classes, so exclude them
    and descend into the definition instead.
    """
    classes: set[str] = set()
    subgraph_ids: set[str] = set()
    if isinstance(workflow, dict) and isinstance(workflow.get("nodes"), list):
        for n in workflow["nodes"]:
            if n.get("type"):
                classes.add(n["type"])
        for sg in workflow.get("definitions", {}).get("subgraphs", []):
            if sg.get("id"):
                subgraph_ids.add(sg["id"])
            for n in sg.get("nodes", []):
                if n.get("type"):
                    classes.add(n["type"])
    elif isinstance(workflow, dict):
        for n in workflow.values():
            if isinstance(n, dict) and n.get("class_type"):
                classes.add(n["class_type"])
    virtual = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode", "Reroute (rgthree)"}
    return {c for c in classes if c not in virtual and c not in subgraph_ids}


@mcp.tool()
async def find_missing_nodes(name: str = "", pack: str = "", source: str = "online") -> str:
    """Diff a template's nodes against what's installed, and resolve each missing
    node to the pack that provides it (via ComfyUI-Manager's registry mapping).

    Fetches the template (same args as get_template), lists the node classes it
    uses, subtracts what /object_info already has, and for each missing class
    reports the installable pack id to pass to install_node_pack. Read-only.
    """
    # fetch template
    if source == "installed":
        if not pack:
            return "source='installed' needs a pack."
        url = f"/api/workflow_templates/{pack}/{name}.json"
        async with _client() as c:
            data = (await c.get(url)).json()
    else:
        async with httpx.AsyncClient(timeout=20.0) as c:
            data = (await c.get(f"{_TPL_BASE}/templates/{name}.json")).json()
    needed = _node_classes(data)
    if not needed:
        return f"Template '{name}' has no resolvable node classes."

    async with _client() as c:
        installed = set((await c.get("/object_info")).json())
        mappings = (await c.get("/customnode/getmappings?mode=cache")).json()
        catalog = (await c.get("/customnode/getlist?mode=cache")).json()
    packs = catalog.get("node_packs", catalog) if isinstance(catalog, dict) else {}

    missing = sorted(needed - installed)
    if not missing:
        return f"All {len(needed)} node classes in '{name}' are already installed. Ready to build/run."

    # class -> source key (url or id) via getmappings
    def resolve(cls: str) -> tuple[str, str] | None:
        for src, val in mappings.items():
            names = val[0] if isinstance(val, list) and val else []
            if cls in names:
                title = val[1].get("title_aux", src) if len(val) > 1 and isinstance(val[1], dict) else src
                return src, title
        return None

    # source key -> installable CNR id via getlist (match id / reference / repository / files)
    def to_pack_id(src: str) -> str | None:
        if isinstance(packs, dict) and src in packs:
            return src
        if isinstance(packs, dict):
            for pid, info in packs.items():
                refs = {info.get("reference"), info.get("repository")} | set(info.get("files", []) or [])
                if src in refs or src.rstrip("/") in {str(r).rstrip("/") for r in refs if r}:
                    return pid
        return None

    lines, unresolved = [], []
    for cls in missing:
        r = resolve(cls)
        if not r:
            unresolved.append(cls)
            continue
        src, title = r
        pid = to_pack_id(src)
        if pid:
            lines.append(f"  {cls}  ->  pack id '{pid}'  ({title})")
        else:
            lines.append(f"  {cls}  ->  {title}  [{src}] (not in CNR registry; install by git url)")
    out = [f"{len(missing)} missing node class(es) in '{name}':", *lines]
    if unresolved:
        out.append("\nCould not resolve to a pack (search ComfyUI-Manager manually): " + ", ".join(unresolved))
    out.append("\nInstall with install_node_pack(pack_id), then restart_comfyui, then re-check.")
    return "\n".join(out)


@mcp.tool()
async def install_node_pack(pack_id: str, version: str = "latest") -> str:
    """Install a custom-node pack by its ComfyUI-Manager registry id (from
    find_missing_nodes) — trusted registry only, no arbitrary code.

    Queues the install, starts the queue, and polls until done. A ComfyUI RESTART
    is required afterward before /object_info reflects the new nodes — call
    restart_comfyui, then re-query. Fails clearly if Manager's security level
    blocks API installs.
    """
    import anyio

    async with _client() as c:
        # Manager's handler reads channel/mode via direct key access — omitting
        # them is a KeyError -> HTTP 500. selected_version drives `<id>@<ver>`.
        payload = {
            "id": pack_id,
            "version": version,
            "selected_version": version,
            "skip_post_install": False,
            "channel": "default",
            "mode": "cache",
        }
        r = await c.post("/manager/queue/install", json=payload)
        if r.status_code == 403:
            return ("Blocked by ComfyUI-Manager security level. Lower it (Manager settings) "
                    "or install this pack manually on the ComfyUI host, then restart.")
        if r.status_code != 200:
            return f"Install request failed (HTTP {r.status_code}): {r.text[:300]}"
        await c.post("/manager/queue/start")
        status = {}
        with anyio.move_on_after(180):
            while True:
                status = (await c.get("/manager/queue/status")).json()
                if status.get("is_processing") is False and status.get("done_count", 0) >= status.get("total_count", 0):
                    break
                await anyio.sleep(2.0)
    return (
        f"Queued + processed install of '{pack_id}' (status: {json.dumps(status)[:200]}).\n"
        "RESTART REQUIRED: call restart_comfyui, then re-run find_missing_nodes / get_node to confirm "
        "the new nodes registered before building."
    )


@mcp.tool()
async def install_model(name: str) -> str:
    """Download a model from the catalog by its exact name (from search_models),
    into the correct models/<type>/ folder, via ComfyUI-Manager.

    Trusted catalog only — Manager whitelists the source. Unlike nodes, models do
    NOT need a ComfyUI restart (loaders re-scan the folder); once it completes,
    verify with list_models. Large models can take a while — the download
    continues server-side even if this call's poll window ends.
    """
    import anyio

    async with _client() as c:
        catalog = (await c.get("/externalmodel/getlist?mode=cache")).json().get("models", [])
        item = next((m for m in catalog if m.get("name") == name), None)
        if not item:
            return f"No catalog model named {name!r}. Use search_models for exact names."
        if str(item.get("installed", "")).lower() == "true":
            return f"'{name}' is already installed ({item.get('save_path')}/{item.get('filename')})."
        r = await c.post("/manager/queue/install_model", json=item)
        if r.status_code == 403:
            return "Blocked by ComfyUI-Manager security level. Lower it, or download the model manually."
        if r.status_code != 200:
            return f"Model install request failed (HTTP {r.status_code}): {r.text[:200]}"
        await c.post("/manager/queue/start")
        status = {}
        with anyio.move_on_after(600):
            while True:
                status = (await c.get("/manager/queue/status")).json()
                if status.get("is_processing") is False and status.get("done_count", 0) >= status.get("total_count", 0):
                    break
                await anyio.sleep(3.0)
    return (
        f"Downloading '{name}' -> {item.get('save_path')}/{item.get('filename')} "
        f"({item.get('size', '?')}; status: {json.dumps(status)[:150]}).\n"
        "No restart needed for models — verify it's available with list_models "
        "(re-run if a large download is still finishing server-side)."
    )


@mcp.tool()
async def restart_comfyui() -> str:
    """Restart ComfyUI (via ComfyUI-Manager) so newly installed nodes register in
    /object_info. The server is briefly unavailable; poll check_comfyui after.
    """
    try:
        async with _client() as c:
            await c.post("/manager/reboot")
    except Exception:  # noqa: BLE001
        pass  # the reboot drops the connection — expected
    return ("Restart triggered. ComfyUI is coming back up — wait a few seconds, then call "
            "check_comfyui to confirm it's live and the new nodes are registered.")


# --------------------------------------------------------------------------- #
# RUN
# --------------------------------------------------------------------------- #
@mcp.tool()
async def submit_workflow(workflow: dict, client_id: str = "comfy-mcp") -> str:
    """Queue an API-format workflow for execution (loop step 2: RUN).

    `workflow` is the flat API/prompt-format dict: {node_id: {class_type, inputs}}.
    Do NOT pass litegraph/UI format here.

    On success: returns the prompt_id — then call get_result to fetch outputs and
    get_image to LOOK at them. Running with zero errors means the graph is VALID,
    not CORRECT — you still have to inspect the pixels.
    On failure: returns node_errors keyed by node id. That is NOT an iteration —
    read the error, fix that specific node, and re-submit until it executes.
    """
    async with _client() as c:
        r = await c.post("/prompt", json={"prompt": workflow, "client_id": client_id})
    if r.status_code == 200:
        pid = r.json().get("prompt_id")
        return (
            f"Queued. prompt_id={pid}\n"
            "Now: get_result(prompt_id) for output filenames, then get_image to "
            "actually LOOK. Zero node_errors = valid, not correct — inspect the "
            "output against the brief and name any concrete defect before deciding."
        )
    try:
        err = r.json()
    except Exception:  # noqa: BLE001
        err = r.text
    return (
        f"REJECTED (HTTP {r.status_code}). This is not an iteration — fix the "
        f"named node(s) and re-submit:\n{json.dumps(err, indent=2) if isinstance(err, dict) else err}"
    )


@mcp.tool()
async def get_result(prompt_id: str, timeout_s: float = 120.0) -> str:
    """Poll /history for a submitted prompt and return its output files (loop
    step 3: LOOK — part 1, find what was produced).

    Blocks up to timeout_s for the run to finish. Returns each output's
    filename / subfolder / type — feed those to get_image to view the pixels.
    """
    import anyio

    deadline_hit = True
    async with _client() as c:
        with anyio.move_on_after(timeout_s):
            while True:
                hist = (await c.get(f"/history/{prompt_id}")).json()
                if prompt_id in hist:
                    deadline_hit = False
                    break
                await anyio.sleep(1.0)
    if deadline_hit:
        return f"Still running after {timeout_s}s. Call get_result again, or check get_queue."

    record = hist[prompt_id]
    outputs = record.get("outputs", {})
    files: list[dict[str, str]] = []
    for node_out in outputs.values():
        for key in ("images", "gifs", "videos"):
            for item in node_out.get(key, []):
                files.append(
                    {
                        "filename": item.get("filename", ""),
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    }
                )
    if not files:
        return f"Run finished but produced no image/video outputs. Check the graph has a Save/Preview node. Raw outputs: {json.dumps(outputs)[:500]}"

    # ComfyUI caches nodes whose inputs didn't change, so a one-param edit only
    # re-executes DOWNSTREAM of that node — iterations are cheap on purpose, and
    # stay cheap only if you keep seeds fixed. Surface it so the model knows.
    cached: list = []
    for msg in record.get("status", {}).get("messages", []):
        if isinstance(msg, list) and len(msg) > 1 and msg[0] == "execution_cached":
            cached = (msg[1] or {}).get("nodes", []) or []
    cache_note = (
        f"\n\n{len(cached)} node(s) served from cache (unchanged upstream) — only the nodes "
        "downstream of your edit actually re-ran. Keep seeds FIXED between passes so this "
        "holds: it makes each iteration cheap, and it isolates your one change as the only "
        "variable (a re-rolled seed means you learn nothing from the diff)."
        if cached
        else ""
    )

    return (
        f"{len(files)} output(s) for {prompt_id}:\n"
        + json.dumps(files, indent=2)
        + cache_note
        + "\n\nNEXT (do not stop here): call get_image on each and LOOK. Compare against your "
        "best-so-far with compare_images(mode='difference') — identical areas read flat gray, "
        "so drift you'd never catch by eye pops out. Then either "
        "(a) name ONE concrete defect vs the brief — six fingers, drifted background, "
        "hard matte edge, over-strong effect, wrong count — change exactly ONE "
        "parameter and re-submit; or (b) if you genuinely cannot name a defect, "
        "declare the brief met and present the result for sign-off. A green run is "
        "valid, not correct — decide by looking, never by a single metric. "
        "RATCHET: call loop_record(run_id, change, outcome, graph) every pass. On a "
        "regression it hands you the best graph back — revert to it and try a different "
        "change instead of building on the regression. Your best-so-far lives in the run, "
        "not in your context, so it survives compaction."
    )


@mcp.tool()
async def get_image(filename: str, subfolder: str = "", image_type: str = "output") -> Image:
    """Fetch a rendered output so you can LOOK at it (loop step 3: LOOK — part 2).

    Returns the actual image to the model. This is the step that makes the loop
    work: don't declare a workflow done off a green run — view the pixels, judge
    them against the brief, then change one thing and re-run.
    """
    async with _client() as c:
        r = await c.get(
            "/view",
            params={"filename": filename, "subfolder": subfolder, "type": image_type},
        )
    r.raise_for_status()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    fmt = {"jpg": "jpeg", "jpeg": "jpeg", "webp": "webp"}.get(ext, "png")
    return Image(data=r.content, format=fmt)


@mcp.tool()
async def upload_image(path: str, overwrite: bool = True) -> str:
    """Upload a local image into ComfyUI's input/ dir so a LoadImage node can use it.

    Returns the name to reference in the workflow. (Video nodes like VHS_LoadVideo*
    read files you place directly in ComfyUI's input/ dir instead.)
    """
    p = Path(path)
    if not p.is_file():
        return f"No file at {path}."
    async with _client() as c:
        files = {"image": (p.name, p.read_bytes())}
        data = {"overwrite": "true" if overwrite else "false"}
        r = await c.post("/upload/image", files=files, data=data)
    if r.status_code != 200:
        return f"Upload failed (HTTP {r.status_code}): {r.text}"
    name = r.json().get("name", p.name)
    return f"Uploaded as '{name}'. Reference it in a LoadImage node's image input."


# --------------------------------------------------------------------------- #
# CONTROL
# --------------------------------------------------------------------------- #
@mcp.tool()
async def system_stats() -> str:
    """Report device / VRAM usage — useful when tuning resolution/batch or after an OOM."""
    async with _client() as c:
        stats = (await c.get("/system_stats")).json()
    return json.dumps(stats, indent=2)


@mcp.tool()
async def get_queue() -> str:
    """Show what's running and pending in ComfyUI's execution queue."""
    async with _client() as c:
        q = (await c.get("/queue")).json()
    running = len(q.get("queue_running", []))
    pending = len(q.get("queue_pending", []))
    return f"Queue: {running} running, {pending} pending.\n{json.dumps(q, indent=2)[:1500]}"


@mcp.tool()
async def interrupt() -> str:
    """Cancel the currently executing prompt."""
    async with _client() as c:
        await c.post("/interrupt")
    return "Interrupt sent."


# --------------------------------------------------------------------------- #
# LOOK — comparisons and objective gates
#
# The loop tells the model to diff outputs and to gate on an objective test where
# the brief has one. Through MCP there is no shell for ffmpeg, so without these
# that instruction is unexecutable and every judgement collapses back to vibes.
# --------------------------------------------------------------------------- #
async def _fetch_view(filename: str, subfolder: str = "", image_type: str = "output") -> bytes:
    async with _client() as c:
        r = await c.get(
            "/view",
            params={"filename": filename, "subfolder": subfolder, "type": image_type},
        )
    r.raise_for_status()
    return r.content


@mcp.tool()
async def compare_images(
    filename_a: str,
    filename_b: str,
    mode: str = "side_by_side",
    subfolder_a: str = "",
    subfolder_b: str = "",
    amplify: float = 1.0,
) -> Image:
    """See what changed between two passes (loop step 3: LOOK — the comparison).

    mode="side_by_side": both outputs on one canvas — what moved, at a glance.
    mode="difference":   0.5 + 0.5*(a-b) — identical regions read FLAT MID-GRAY and
                         only real changes pop. This is how you answer "did the
                         background actually stay put?", which the eye is bad at.
                         Raise `amplify` (e.g. 4.0) to surface subtle drift.

    Use it every pass against your best-so-far: a change that altered more than you
    intended is a regression even if the new bit looks nice.
    """
    a = await _fetch_view(filename_a, subfolder_a)
    b = await _fetch_view(filename_b, subfolder_b)
    data = (
        imaging.side_by_side(a, b)
        if mode == "side_by_side"
        else imaging.difference(a, b, amplify=amplify)
    )
    return Image(data=data, format="png")


@mcp.tool()
async def image_diff_stats(
    filename_a: str, filename_b: str, subfolder_a: str = "", subfolder_b: str = ""
) -> str:
    """Quantify the change between two passes — the 'I changed only what I meant to' gate.

    Returns mean/max absolute difference and the % of pixels that moved. Pair it
    with compare_images: the picture tells you WHAT changed, this tells you HOW
    MUCH — and catches the case where a 'small tweak' quietly rewrote the frame.
    """
    a = await _fetch_view(filename_a, subfolder_a)
    b = await _fetch_view(filename_b, subfolder_b)
    return json.dumps(imaging.diff_stats(a, b), indent=2)


@mcp.tool()
async def measure_image(filename: str, metric: str = "sharpness", subfolder: str = "") -> str:
    """Score an output objectively, for the ratchet (metric: sharpness | tile_seam | brightness).

    Use ONLY where the brief has an objective test — then feed the score to
    loop_record so the ratchet can't be fooled by a model that wants to be done:

      tile_seam   "seamless texture" — compares the wrap-around join to an interior
                  join. ~1.0 = genuinely tiles; >2 = a real seam. The eye waves this through.
      sharpness   "upscale/restore, add detail" — edge energy. Rises with real detail,
                  falls when a pass just softened the image. Compare ACROSS passes.
      brightness  mean / stddev / p99 — exposure and blown-highlight checks.

    A score is not the judgement. A graph with a great number can still look wrong —
    gate on the metric, decide with your eyes.
    """
    data = await _fetch_view(filename, subfolder)
    if metric == "tile_seam":
        return json.dumps(imaging.tile_seam(data), indent=2)
    if metric == "brightness":
        return json.dumps(imaging.brightness(data), indent=2)
    if metric == "sharpness":
        return json.dumps(
            {"sharpness": imaging.sharpness(data), "score": imaging.sharpness(data),
             "note": "higher = more edge detail; compare across passes, not to an absolute"},
            indent=2,
        )
    return f"Unknown metric {metric!r}. Use: sharpness | tile_seam | brightness."


# --------------------------------------------------------------------------- #
# LOOP STATE — the ratchet and the ledger, held outside the model's context
#
# A long loop gets compacted. If best-so-far and the ledger live only in context,
# the ratchet silently stops ratcheting, the model retries changes it already
# rejected, and it can present a regression as final. So they live on disk.
# --------------------------------------------------------------------------- #
@mcp.tool()
async def loop_start(brief: str, gate: str = "") -> str:
    """Open a loop run — do this BEFORE the first submit (loop step 0).

    `brief` is what "right" means, in the user's words; you'll be judged against it.
    `gate` is the objective test IF the brief has one ("must tile seamlessly",
    "exactly 3 apples", "identity preserved") — leave empty for purely aesthetic work.

    Returns a run_id. Pass it to loop_record every pass. This is what makes the
    ratchet real: your best graph is stored HERE, not in your context, so it
    survives compaction and can actually be reverted to.
    """
    run = loopstate.start(brief, gate)
    return (
        f"run_id: {run['run_id']}\nBrief: {brief}\n"
        + (f"Objective gate: {gate}\n" if gate else "")
        + "\nEvery pass: change ONE thing → submit → get_result → get_image → LOOK → "
        "judge vs the brief → loop_record(run_id, change, outcome, graph). Record the "
        "graph on every 'better' — a best you can't restore is not a best."
    )


@mcp.tool()
async def loop_record(
    run_id: str,
    change: str,
    outcome: str,
    graph: dict | None = None,
    score: float | None = None,
    note: str = "",
    outputs: list | None = None,
) -> str:
    """Record a pass and apply the ratchet (loop step 5: DECIDE).

    `change`  the ONE thing you changed this pass ("denoise 0.6 -> 0.45").
    `outcome` your verdict vs the best-so-far: "better" | "worse" | "same".
    `graph`   the API graph you just ran — REQUIRED when outcome is "better", because
              that's what gets stored as the new best and handed back on a revert.
    `score`   an objective score from measure_image, when the brief has a gate. If both
              this pass and the best have one, the NUMBER decides — not your verdict.
              (A model that wants to be finished will call a regression "better".)
    `outputs` this pass's output files, straight from get_result — pass them through so
              loop_report can show what each pass actually looked like.

    On "worse"/"same" you get the best graph back: revert to it and try a DIFFERENT
    change. Never build on a regression — that's how a loop wanders instead of converging.
    """
    try:
        res = loopstate.record(
            run_id, change, outcome, graph=graph, score=score, note=note, outputs=outputs
        )
    except KeyError:
        return f"No run {run_id!r}. Call loop_start first."
    except ValueError as e:
        return str(e)

    run, n = res["run"], res["pass_n"]
    if res["promoted"]:
        return (
            f"Pass {n} recorded — NEW BEST (stored, revertible).\n"
            f"{loopstate.format_ledger(run)}\n\n"
            "Keep going: can you still name a concrete defect? Then change ONE more "
            "thing. If you genuinely cannot, call loop_finish and present for sign-off."
        )

    best = run.get("best")
    if not best:
        return (
            f"Pass {n} recorded ({res['run']['passes'][-1]['outcome']}). No best yet — "
            "nothing to revert to. Record a 'better' pass WITH its graph to set one.\n"
            f"{loopstate.format_ledger(run)}"
        )
    already = loopstate.tried(run)
    return (
        f"Pass {n} recorded as {run['passes'][-1]['outcome']} — REVERT.\n\n"
        f"Best remains pass {best['pass']}. Its graph follows; go back to it and try a "
        f"DIFFERENT change (do not build on this regression).\n\n"
        f"Changes already tried (don't repeat): {already}\n\n"
        f"BEST GRAPH:\n{json.dumps(best['graph'], indent=2)}"
    )


@mcp.tool()
async def loop_best(run_id: str) -> str:
    """Fetch the best-so-far graph — to revert to it, or to deliver it as the final.

    Use this after a compaction, or any time you're unsure the graph in your context
    is still the best one. It is the source of truth; your memory is not.
    """
    b = loopstate.best(run_id)
    if not b:
        return f"No best recorded for {run_id!r} yet."
    return (
        f"Best = pass {b['pass']}"
        + (f" (score {b['score']})" if b.get("score") is not None else "")
        + f" — {b.get('note', '')}\n\n{json.dumps(b['graph'], indent=2)}"
    )


@mcp.tool()
async def loop_ledger(run_id: str) -> str:
    """The append-only loop log: every pass, what changed, what it did.

    Read it after a context compaction to recover the thread — what the brief was,
    what's already been tried (so you don't retry a dead end), and which pass is best.
    This is also the log you hand the user at sign-off; it's the story of how the
    result got good.
    """
    run = loopstate.get(run_id)
    if not run:
        return f"No run {run_id!r}."
    return loopstate.format_ledger(run) + f"\n\nAlready tried: {loopstate.tried(run)}"


@mcp.tool()
async def loop_finish(run_id: str, summary: str = "") -> str:
    """Close the loop at the convergence checkpoint — you can't name a defect anymore.

    Marks the run converged and returns the final ledger + the best graph, ready to
    present. Then STOP and ask the user to approve or request changes; don't keep
    inventing variations to avoid stopping.
    """
    run = loopstate.finish(run_id, summary)
    if not run:
        return f"No run {run_id!r}."
    b = run.get("best")
    out = ["CONVERGED — present this and ask for sign-off.\n", loopstate.format_ledger(run)]
    if summary:
        out.append(f"\nSummary: {summary}")
    if b:
        out.append(f"\nFINAL GRAPH (best = pass {b['pass']}):\n{json.dumps(b['graph'], indent=2)}")
    return "\n".join(out)


@mcp.tool()
async def loop_report(run_id: str, out_path: str = "") -> str:
    """Render the whole run as ONE self-contained HTML page — every pass, what changed,
    what was kept, what was reverted, and the final.

    This is the artifact worth keeping. The final image alone proves nothing; the
    *evidence of convergence* — the passes you threw away — is what shows the loop
    actually worked. Hand it over at the sign-off checkpoint alongside the result.

    Images are downscaled and base64-inlined, so the page renders with ComfyUI off,
    on someone else's machine, or emailed. Writes next to the run state by default;
    set out_path to put it anywhere.
    """
    run = loopstate.get(run_id)
    if not run:
        return f"No run {run_id!r}."

    # Pull each pass's output. A pass whose file is gone just renders without a thumb —
    # a missing image must not take down the report.
    images: dict[int, bytes] = {}
    for p in run.get("passes", []):
        for out in p.get("outputs") or []:
            if not isinstance(out, dict) or not out.get("filename"):
                continue
            try:
                images[p["n"]] = await _fetch_view(
                    out["filename"], out.get("subfolder", ""), out.get("type", "output")
                )
            except Exception:
                pass
            break  # one thumbnail per pass is the story; the rest is noise

    best = run.get("best") or {}
    final = images.get(best.get("pass")) if best else None

    path = Path(out_path) if out_path else loopstate.STATE_DIR / f"{run['run_id']}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.render(run, images, final), encoding="utf-8")

    n = len(run.get("passes", []))
    kept = sum(1 for p in run.get("passes", []) if p.get("kept"))
    return (
        f"Loop report written to {path}\n"
        f"{n} passes ({kept} kept, {n - kept} reverted), {len(images)} output(s) embedded.\n"
        "Self-contained — no external assets, renders anywhere. Show it with the result: "
        "the passes you threw away are what prove the loop converged."
    )


# --------------------------------------------------------------------------- #
# DELIVER — the UI-editable file
# --------------------------------------------------------------------------- #
@mcp.tool()
async def save_workflow(workflow: dict, name: str = "", save: bool = True) -> str:
    """Convert an API graph to UI/litegraph format so a human can open and edit it.

    The loop builds API format because that's what /prompt runs — it is NOT the file
    you drag onto the ComfyUI canvas. Call this when the user asks for the editable
    workflow.

    The result is VERIFIED by converting it back to API format and diffing against
    what you passed in; any mismatch is reported. widgets_values is positional and a
    silent off-by-one shifts parameters — a plausible-but-wrong file is worse than
    none, so if the round-trip doesn't match, fix it before shipping it.

    With save=True and a name, it's written to ComfyUI's workflows dir so it shows up
    in the UI's workflow list.
    """
    async with _client() as c:
        object_info = (await c.get("/object_info")).json()

    wf, warnings = api_to_litegraph(workflow, object_info)
    head = f"{len(wf['nodes'])} nodes, {len(wf['links'])} links."
    if warnings:
        head += "\n\n⚠ ROUND-TRIP MISMATCH — do NOT ship this file as-is:\n - " + "\n - ".join(
            warnings[:12]
        )
    else:
        head += " Round-trip verified: converts back to the exact API graph you gave me."

    saved = ""
    if save and name:
        fname = name if name.endswith(".json") else f"{name}.json"
        try:
            async with _client() as c:
                r = await c.post(
                    f"/userdata/workflows%2F{fname}",
                    content=json.dumps(wf).encode(),
                    headers={"Content-Type": "application/json"},
                )
            saved = (
                f"\n\nSaved to ComfyUI as workflows/{fname} — open it from the UI's workflow list."
                if r.status_code < 300
                else f"\n\nCouldn't save to ComfyUI (HTTP {r.status_code}); the JSON is below — save it yourself."
            )
        except Exception as e:
            saved = f"\n\nCouldn't save to ComfyUI ({e}); the JSON is below — save it yourself."

    return f"{head}{saved}\n\n{json.dumps(wf, indent=2)}"


# --------------------------------------------------------------------------- #
# PROMPTS — the loop methodology, so any client can pull it in
# --------------------------------------------------------------------------- #
@mcp.prompt(title="ComfyUI build-and-loop method")
def comfy_loop() -> str:
    """The full autonomous build→run→look→critique→fix loop prompt.

    Load this at the start of a ComfyUI task to give the model the whole method:
    discover from the live API, build API format, validate by executing, then
    iterate on the rendered output until it meets the brief.
    """
    return _read_doc("COMFYUI_WORKFLOW_LOOP_PROMPT.md")


@mcp.prompt(title="ComfyUI workflow skill")
def comfy_skill() -> str:
    """The compact skill version of the method (discover → build → validate → iterate)."""
    return _read_doc("SKILL.md")


# --------------------------------------------------------------------------- #
# RESOURCES — live truth + docs
# --------------------------------------------------------------------------- #
@mcp.resource("comfyui://object_info")
async def object_info_resource() -> str:
    """The live, full /object_info dump — every installed node's exact interface."""
    async with _client() as c:
        return (await c.get("/object_info")).text


@mcp.resource("comfyui://loop-method")
def loop_method_resource() -> str:
    """The build-and-loop prompt as a readable resource."""
    return _read_doc("COMFYUI_WORKFLOW_LOOP_PROMPT.md")


@mcp.resource("comfyui://skill")
def skill_resource() -> str:
    """The ComfyUI workflow skill as a readable resource."""
    return _read_doc("SKILL.md")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
