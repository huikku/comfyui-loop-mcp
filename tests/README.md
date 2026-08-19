# comfyui-loop-mcp tests

Mostly integration tests — they drive the MCP server against a **live ComfyUI**,
because the value is in exercising the real API. The one exception is graph
conversion, which is pure logic and gets a pure test.

## Offline (no ComfyUI needed)

```bash
python tests/test_reroute.py
```

```bash
python tests/test_video.py
```

```bash
python tests/test_subgraph.py
```

```bash
python tests/test_validate.py
```

```bash
python tests/test_http_tools.py
```

The video LOOK tools, exercised against clips synthesized with `lavfi` — no
ComfyUI, no fixtures in the repo. Skips (rather than fails) without ffmpeg,
which is the same condition the tools themselves degrade on.

Three cases carry their weight. `frame()` must index by FRAME and different
indices must actually differ — a test that only asserted "returns a PNG" would
pass while indexing was silently broken, and indexing by timestamp is the exact
bug the module exists to prevent. `temporal_stats()` must rank a static clip
below a moving one, or the number doesn't mean what it claims. And it must
TERMINATE: the first implementation piped concatenated PPMs and re-opened one
buffer in a loop, which spins forever because Pillow doesn't reliably advance
the stream — a hang reads as a crashed client, which is worse than a wrong
answer.

`test_subgraph.py` covers the other half of conversion: a **subgraph instance
must be expanded, not skipped**. A subgraph is a canvas-only abstraction — skip it
and the graph has a hole where the pipe was, and the consumer downstream points at
a node that no longer exists. Roughly half the shipped catalog is authored this
way. Covers the basic boundary crossing in both directions, two instances of one
definition not colliding, nesting two levels deep, a widget promoted onto the
instance reaching the interior input, interior links serialized as objects rather
than arrays, and a definition that contains itself terminating instead of
recursing forever. The last case in the file pins a bug the subgraph work exposed:
a widget CONVERTED to an input still occupies its `widgets_values` slot, and
skipping that slot shifts every later widget by one — an `EmptySD3LatentImage`
with width and height wired hands 1024 to `batch_size`. It runs, which is what
makes it dangerous.

`test_http_tools.py` runs the control/verify tools against a **stub ComfyUI** —
a threaded `http.server` answering the same routes with the same payload shapes.
They are thin tools, which is exactly why they go wrong quietly: a queue entry read
at the wrong index, a log payload that is a list of dicts rather than lines, an
error record parsed as "finished but produced no outputs". None of that needs a GPU
to get wrong, so none of it needs one to test. It pins behaviour a caller depends
on rather than wording — a failed run is reported as a failure, cancelling a QUEUED
job does not interrupt the RUNNING one, and a sweep writes its value → prompt_id
table into durable state (in a temp dir, never the real ledger).

`test_validate.py` pins the pre-flight's one job: separating "install something"
from "fix the graph". `/prompt` reports both as the same red box, one per submit.
A checker that finds problems but mislabels them is worse than none — it sends the
model off installing a pack when the checkpoint filename was simply misspelled.

`litegraph_to_api` must **follow links through `Reroute` nodes**. Reroute is a
frontend-only passthrough with no backend class, so a link pointing at one has to
be rewired to the real producer — otherwise the API graph references a node id
that doesn't exist and `/prompt` fails with an error nowhere near the cause.
Covers a single reroute, a chain (what a reroute *bus* produces), a dangling one,
and a cycle. Fails 5/7 against the pre-fix implementation.

## Prerequisites

- `pip install -e .` (installs `comfyui-loop-mcp` + deps)
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

`integration_loop.py` (23 checks): everything that makes this an MCP for
*looping* rather than for driving ComfyUI once — `upload_image`,
`flowzip_to_api`, `inflate_workflow`, `run_template` (with overrides),
`measure_image`, `compare_images` (side-by-side + difference),
`image_diff_stats`, the whole ratchet (`loop_start` → `loop_record` →
`loop_best` → `loop_ledger` → `loop_report` → `loop_finish`), `interrupt`,
`save_workflow`, and `restart_comfyui`'s **failure** path (run against a
throwaway 404 stub, so nothing real is touched — it must report the failure, not
claim the restart happened).

It runs a **real two-pass loop**, and its load-bearing check is that an
**objective score overrules a wrong verdict**: pass 2 is recorded as `"better"`
while being handed a worse score, and the ledger must reject the claim, keep
pass 1 as best, and hand back its graph. A ratchet that believes whatever the
agent tells it is not a ratchet — and over a long run the agent is precisely the
component that goes wrong. Loop state is written to a temp dir, never to the
real `~/.comfyui-loop-mcp`.

## Mutating suite — gated behind an env var

`integration_mutating.py` covers the three tools that change the ComfyUI host:
`install_model`, `install_node_pack`, and `restart_comfyui`'s success path. It
downloads a real model, installs a real pack, and restarts ComfyUI twice, so it
**refuses to run** unless you opt in:

```bash
COMFY_LOOP_ALLOW_MUTATION=1 COMFYUI_URL=http://localhost:8188 \
    python tests/integration_mutating.py
```

Without the env var it prints what it *would* do and exits 0. The tools are
judged by ground truth, not by their own success messages: `install_model` only
passes if the file appears in `UpscaleModelLoader`'s enum, and
`install_node_pack` only if its nodes appear in `/object_info` after the restart.

Between the two automated suites and this gated one, **all 32 tools have a
passing test.**

Verified end-to-end on linuxdev (RTX 4090, ComfyUI 0.25.0, ComfyUI-Manager
V3.41): 6/6.

**Bug found + fixed during the original pass:** `install_node_pack` returned HTTP
500 because Manager's `/manager/queue/install` reads `channel`/`mode` by direct
key access; those are now sent (`channel="default"`, `mode="cache"`). Without
live testing this would have shipped broken.

To run the mutating suite manually (installs a real pack — do it deliberately):

```python
# python - <<'PY'  (with COMFYUI_URL set)
import asyncio, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    p = StdioServerParameters(command="comfyui-loop-mcp", env={**os.environ})
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print(await s.call_tool("install_node_pack", {"pack_id": "rgthree-comfy"}))
            print(await s.call_tool("restart_comfyui", {}))
asyncio.run(main())
# PY
```
