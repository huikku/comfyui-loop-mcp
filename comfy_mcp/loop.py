"""Durable loop state — the ratchet and the ledger, held OUTSIDE the model's context.

The loop's whole premise is "keep iterating until it's right," which means long
runs, which means context compaction. The one thing that must not live in the
model's memory is the loop's memory: the moment it's compacted away, the ratchet
silently stops ratcheting (there is nothing to revert *to*), the model re-tries
changes it already rejected, and it can hand back a regression as final because
it no longer remembers the better pass.

So the best-so-far GRAPH and the append-only ledger live on disk, keyed by run.
Reverting becomes a tool call, not an act of recall.

State dir via COMFY_MCP_STATE_DIR (default ~/.comfy-mcp/runs).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

STATE_DIR = Path(
    os.environ.get("COMFY_MCP_STATE_DIR", str(Path.home() / ".comfy-mcp" / "runs"))
)

# An outcome is the model's verdict on a pass, relative to the best-so-far.
OUTCOMES = ("better", "worse", "same")


def _path(run_id: str) -> Path:
    # run_id is generated here, but never trust it off the wire as a path.
    safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
    return STATE_DIR / f"{safe}.json"


def _load(run_id: str) -> dict[str, Any] | None:
    p = _path(run_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(run: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(run["run_id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, indent=2), encoding="utf-8")
    tmp.replace(p)  # atomic — a crash mid-write can't corrupt the ledger


def start(brief: str, gate: str = "") -> dict[str, Any]:
    """Open a run: record the brief and (optionally) the objective gate."""
    run = {
        "run_id": uuid.uuid4().hex[:12],
        "brief": brief,
        "gate": gate,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "running",
        "passes": [],
        "best": None,  # {"pass": n, "graph": {...}, "score": float|None, "note": str}
    }
    _save(run)
    return run


def record(
    run_id: str,
    change: str,
    outcome: str,
    graph: dict | None = None,
    score: float | None = None,
    note: str = "",
    outputs: list | None = None,
) -> dict[str, Any]:
    """Append a pass and apply the ratchet.

    `outcome` is the verdict vs the best-so-far. "better" promotes this pass's
    graph to best (which requires passing `graph` — a best you can't restore is
    not a best). "worse"/"same" leaves best untouched, and the caller is handed
    the best graph back so it can revert in one step.

    A `score` (higher = better), when the brief has an objective gate, overrides
    a mistaken "better": the eye is fallible and this is the point of a ratchet.
    """
    run = _load(run_id)
    if run is None:
        raise KeyError(run_id)

    outcome = outcome.lower().strip()
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")

    best = run.get("best")
    # An objective score, when present on both sides, is the arbiter — not the verdict.
    if score is not None and best and best.get("score") is not None:
        outcome = "better" if score > best["score"] else ("same" if score == best["score"] else "worse")

    n = len(run["passes"]) + 1
    promoted = False
    if outcome == "better" and graph is not None:
        run["best"] = {"pass": n, "graph": graph, "score": score, "note": note or change}
        promoted = True

    run["passes"].append(
        {
            "n": n,
            "change": change,
            "outcome": outcome,
            "score": score,
            "note": note,
            "outputs": outputs or [],
            "kept": promoted,
        }
    )
    _save(run)
    return {"run": run, "promoted": promoted, "pass_n": n}


def best(run_id: str) -> dict[str, Any] | None:
    run = _load(run_id)
    return run.get("best") if run else None


def get(run_id: str) -> dict[str, Any] | None:
    return _load(run_id)


def finish(run_id: str, summary: str = "") -> dict[str, Any] | None:
    run = _load(run_id)
    if run is None:
        return None
    run["status"] = "converged"
    run["summary"] = summary
    _save(run)
    return run


def tried(run: dict[str, Any]) -> list[str]:
    """Changes already attempted — so a compacted model doesn't retry a dead end."""
    return [p["change"] for p in run.get("passes", [])]


def format_ledger(run: dict[str, Any]) -> str:
    """The loop log: one line per pass, what changed and what it did."""
    lines = []
    for p in run.get("passes", []):
        mark = "✓ kept" if p["kept"] else f"✗ {p['outcome']}"
        score = f" [score {p['score']}]" if p.get("score") is not None else ""
        note = f" — {p['note']}" if p.get("note") else ""
        lines.append(f"  pass {p['n']}  {p['change']}{score}  → {mark}{note}")
    if not lines:
        lines = ["  (no passes recorded yet)"]
    b = run.get("best")
    head = [
        f"Run {run['run_id']} — {run.get('status')}",
        f"Brief: {run.get('brief')}",
    ]
    if run.get("gate"):
        head.append(f"Objective gate: {run['gate']}")
    head.append(f"Best so far: pass {b['pass']} — {b.get('note', '')}" if b else "Best so far: (none yet)")
    return "\n".join(head) + "\n\nLoop log:\n" + "\n".join(lines)
