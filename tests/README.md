# comfy-mcp tests

Mostly integration tests — they drive the MCP server against a **live ComfyUI**,
because the value is in exercising the real API. The one exception is graph
conversion, which is pure logic and gets a pure test.

## Offline (no ComfyUI needed)

```bash
python tests/test_reroute.py
```

`litegraph_to_api` must **follow links through `Reroute` nodes**. Reroute is a
frontend-only passthrough with no backend class, so a link pointing at one has to
be rewired to the real producer — otherwise the API graph references a node id
that doesn't exist and `/prompt` fails with an error nowhere near the cause.
Covers a single reroute, a chain (what a reroute *bus* produces), a dangling one,
and a cycle. Fails 5/7 against the pre-fix implementation.

## Prerequisites

- `pip install -e .` (installs `comfy-mcp` + deps)
- A reachable ComfyUI with an SD1.5 checkpoint (set `TEST_CKPT` to override the
  default `v1-5-pruned-emaonly.safetensors`).
- Network access to the GitHub template catalog (for template/bench tests).
- `COMFYUI_URL` pointed at your ComfyUI (default `http://localhost:8188`; for a
  remote box, open an SSH tunnel first — see the main README).

## Safe suites (no mutations)

```bash
COMFYUI_URL=http://localhost:8188 python tests/integration_smoke.py   # 17 checks — discovery + one generation
COMFYUI_URL=http://localhost:8188 python tests/integration_loop.py    # 21 checks — the loop, the LOOK tools, conversion
COMFYUI_URL=http://localhost:8188 python tests/bench.py               # compression/conversion metrics
```

Together the two suites cover **29 of the 32 tools**. The three that aren't
covered are exactly the three that mutate the ComfyUI host — see below.

`integration_smoke.py` (17 checks): tool registration, `check_comfyui`,
`list_nodes` (compact), `get_node`, `list_models`, `search_models`,
`search_templates`, `get_template` (flowzip), `find_missing_nodes`,
`template_slots`, error robustness, all three resources, `get_queue`,
`system_stats`, and a full text-to-image submit→result→get_image.

`integration_loop.py` (21 checks): everything that makes this an MCP for
*looping* rather than for driving ComfyUI once — `upload_image`,
`flowzip_to_api`, `inflate_workflow`, `run_template` (with overrides),
`measure_image`, `compare_images` (side-by-side + difference),
`image_diff_stats`, the whole ratchet (`loop_start` → `loop_record` →
`loop_best` → `loop_ledger` → `loop_report` → `loop_finish`), `interrupt`, and
`save_workflow`.

It runs a **real two-pass loop**, and its load-bearing check is that an
**objective score overrules a wrong verdict**: pass 2 is recorded as `"better"`
while being handed a worse score, and the ledger must reject the claim, keep
pass 1 as best, and hand back its graph. A ratchet that believes whatever the
agent tells it is not a ratchet — and over a long run the agent is precisely the
component that goes wrong. Loop state is written to a temp dir, never to the
real `~/.comfy-mcp`.

## Mutating paths — manual, and verified once on a live box

These change the ComfyUI host (install code/files, restart the server), so they
are **not** in the automated suite — run them deliberately. Verified end-to-end
on linuxdev (RTX 4090, ComfyUI 0.25.0, ComfyUI-Manager V3.41):

| Path | Result | Notes |
|---|---|---|
| `install_model("RealESRGAN x2")` → `list_models` | ✅ PASS | 67 MB; appeared in `UpscaleModelLoader` enum; no restart needed |
| `restart_comfyui` → `check_comfyui` | ✅ PASS | ~13 s recovery (ComfyUI under pm2 auto-restarts) |
| `install_node_pack("rgthree-comfy")` → `restart_comfyui` → `list_nodes` | ✅ PASS | 24 `(rgthree)` nodes registered after restart |

**Bug found + fixed during this pass:** `install_node_pack` returned HTTP 500
because Manager's `/manager/queue/install` reads `channel`/`mode` by direct key
access; those are now sent (`channel="default"`, `mode="cache"`). Without live
testing this would have shipped broken.

To re-run a mutating check manually (installs a real pack — do it deliberately):

```python
# python - <<'PY'  (with COMFYUI_URL set)
import asyncio, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    p = StdioServerParameters(command="comfy-mcp", env={**os.environ})
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print(await s.call_tool("install_node_pack", {"pack_id": "rgthree-comfy"}))
            print(await s.call_tool("restart_comfyui", {}))
asyncio.run(main())
# PY
```
