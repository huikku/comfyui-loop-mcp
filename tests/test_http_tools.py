# Copyright (c) 2026 John Huikku · Alienrobot LLC · alienrobot.com
# SPDX-License-Identifier: MIT
"""Offline test: the control/verify tools, against a canned ComfyUI.

These tools are thin, which is exactly why they go wrong quietly — a queue entry
read at the wrong index, a log payload that is a list of dicts rather than lines,
an error record parsed as "no outputs". None of that needs a GPU to get wrong, so
none of it needs one to test: a stub server answers the same routes with the same
shapes ComfyUI uses.

What it pins is behaviour a caller depends on, not wording: that a failed run is
reported as a failure, that cancelling a QUEUED job doesn't interrupt the RUNNING
one, that a sweep writes its value -> prompt_id table into durable state.

Run:  python tests/test_http_tools.py     (no ComfyUI needed)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request, times 30, is not signal

OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["sd15.safetensors"]]}},
        "output": ["MODEL", "CLIP", "VAE"],
    },
    "KSampler": {"input": {"required": {
        "model": ["MODEL"], "seed": ["INT", {"control_after_generate": True}],
        "steps": ["INT", {"min": 1, "max": 100}], "denoise": ["FLOAT", {"min": 0.0, "max": 1.0}],
    }}},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True},
}

HISTORY = {
    "done-1": {"outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}},
               "status": {"status_str": "success", "messages": []}},
    "died-1": {"outputs": {}, "status": {"status_str": "error", "messages": [
        ["execution_error", {"node_id": "7", "node_type": "KSampler",
                             "exception_type": "torch.OutOfMemoryError",
                             "exception_message": "CUDA out of memory. Tried to allocate 2 GiB",
                             "traceback": ["frame one", "frame two"]}]]}},
}
QUEUE = {
    "queue_running": [[0, "running-1", {}, {}, []]],
    "queue_pending": [[1, "pending-1", {}, {}, []], [2, "pending-2", {}, {}, []]],
}
# What the stub install looks like — tests flip these to walk a caller through the
# states a fresh box actually passes through: no Manager, no weights, wrong torch.
BOX = {"manager": True, "device_type": "cuda", "ckpts": ["sd15.safetensors"]}
POSTS: list[tuple[str, dict]] = []
SUBMITS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes after every response, and httpx pools connections — reusing
    # one the stub already hung up on surfaces as RemoteProtocolError, which reads
    # like a bug in the server under test rather than in the fixture.
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/object_info":
            info = json.loads(json.dumps(OBJECT_INFO))
            info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [BOX["ckpts"]]
            return self._send(info)
        if path == "/system_stats":
            return self._send({"system": {"comfyui_version": "0.3.99", "python_version": "3.12.1 (main)",
                                          "pytorch_version": "2.6.0"},
                               "devices": [{"name": "cuda:0 NVIDIA", "type": BOX["device_type"],
                                            "vram_total": 24_000_000_000,
                                            "vram_free": 21_000_000_000}]})
        if path == "/queue":
            return self._send(QUEUE)
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[-1]
            return self._send({pid: HISTORY[pid]} if pid in HISTORY else {})
        if path == "/manager/version":
            if not BOX["manager"]:
                return self._send({}, 404)
            body = b'"3.40"'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/internal/logs/raw":
            return self._send({"entries": [{"m": "Starting server"}, {"m": "ERROR: node import failed"},
                                           {"m": "got prompt"}]})
        return self._send({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        POSTS.append((self.path, payload))
        if self.path == "/prompt":
            SUBMITS.append(payload["prompt"])
            return self._send({"prompt_id": f"queued-{len(SUBMITS)}"})
        return self._send({"ok": True})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["COMFYUI_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
STATE = tempfile.mkdtemp(prefix="comfy-loop-httptest-")
os.environ["COMFY_LOOP_STATE_DIR"] = STATE  # never touch the real ledger

from comfy_loop import server as S  # noqa: E402  (env must be set first)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


# --- preflight ------------------------------------------------------------- #
out = run(S.check_comfyui())
check("check_comfyui reports node count and version", "3 nodes installed" in out and "0.3.99" in out, out)
check("check_comfyui reports FREE vram, not just total", "21.0/24.0GB free" in out, out)
check("check_comfyui warns that the queue is busy", "1 running, 2 pending" in out, out)
check("check_comfyui detects ComfyUI-Manager", "ComfyUI-Manager 3.40" in out, out)

# --- check_workflow -------------------------------------------------------- #
good = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
out = run(S.check_workflow(good))
check("check_workflow passes a clean graph", out.startswith("READY"), out[:120])

out = run(S.check_workflow({"1": {"class_type": "CheckpointLoaderSimple",
                                  "inputs": {"ckpt_name": "nope.safetensors"}}}))
check("check_workflow blocks on a model this box doesn't have", "BLOCKER" in out, out[:200])
check("and names the file, not just the node", "nope.safetensors" in out, out[:200])
check("and flags that nothing would be produced", "no output node" in out, out[:400])

# --- job status ------------------------------------------------------------ #
check("job_status: finished run reports its outputs",
      "DONE, 1 output" in run(S.job_status("done-1")))
out = run(S.job_status("died-1"))
check("job_status: a run that died is a FAILURE, not an empty result",
      "RUN FAILED" in out and "KSampler" in out, out[:200])
check("and an OOM says what to do about it", "free_vram" in out, out[:300])
check("job_status: a queued run reports its position",
      "position 2 of 2" in run(S.job_status("pending-2")), run(S.job_status("pending-2")))
check("job_status: the running one is reported as running",
      "RUNNING" in run(S.job_status("running-1")))
check("job_status: an unknown id says so, rather than inventing a state",
      "not in the queue" in run(S.job_status("nonsense")))

# --- cancelling ------------------------------------------------------------ #
POSTS.clear()
run(S.cancel_job("pending-1"))
check("cancel_job on a QUEUED id deletes it and leaves the running job alone",
      POSTS and POSTS[-1][0] == "/queue" and POSTS[-1][1] == {"delete": ["pending-1"]}, str(POSTS))
POSTS.clear()
run(S.cancel_job("running-1"))
check("cancel_job on the RUNNING id interrupts instead",
      POSTS and POSTS[-1][0] == "/interrupt", str(POSTS))

# --- vram + logs ----------------------------------------------------------- #
POSTS.clear()
out = run(S.free_vram())
check("free_vram asks for both the unload and the cache reset",
      POSTS[-1][0] == "/free" and POSTS[-1][1] == {"unload_models": True, "free_memory": True}, str(POSTS))
check("free_vram does not claim the memory is back", "Confirm with system_stats" in out, out)

out = run(S.comfyui_logs(grep="ERROR"))
check("comfyui_logs parses entry objects and filters",
      "node import failed" in out and "Starting server" not in out, out)

# --- get_result on a failed run -------------------------------------------- #
out = run(S.get_result("died-1", timeout_s=2))
check("get_result reports the failing node, not 'no outputs'",
      "RUN FAILED" in out and "produced no image" not in out, out[:200])

# --- loop_sweep ------------------------------------------------------------ #
run_id = run(S.loop_start("test brief")).split("\n")[0].split(": ")[1]
graph = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd15.safetensors"}},
         "2": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": 5, "steps": 20, "denoise": 0.5}}}
SUBMITS.clear()
out = run(S.loop_sweep(run_id, graph, "2", "denoise", [0.3, 0.5, 0.7]))
check("loop_sweep submits one run per value", len(SUBMITS) == 3, f"{len(SUBMITS)} submits")
check("loop_sweep changes ONLY the swept input",
      [s["2"]["inputs"]["denoise"] for s in SUBMITS] == [0.3, 0.5, 0.7]
      and all(s["2"]["inputs"]["seed"] == 5 for s in SUBMITS), json.dumps(SUBMITS)[:200])
check("loop_sweep does not mutate the caller's graph", graph["2"]["inputs"]["denoise"] == 0.5)
ledger = run(S.loop_ledger(run_id))
check("the sweep survives in durable state, not just in the reply",
      "sweep on 2.denoise" in ledger and "queued-3" in ledger, ledger[:300])
check("loop_sweep refuses a value list that is really a grid search",
      "grid search" in run(S.loop_sweep(run_id, graph, "2", "denoise", list(range(12)))))
check("loop_sweep refuses an input that isn't on the node",
      "is not an input" in run(S.loop_sweep(run_id, graph, "2", "nope", [1])))
check("loop_sweep refuses to run without a loop to record into",
      "loop_start" in run(S.loop_sweep("no-such-run", graph, "2", "denoise", [1])))

# --- the states a fresh box passes through -------------------------------- #
#
# A ComfyUI can be up, fully functional, and unable to render anything: no
# weights on disk, or a CPU torch wheel that works and is 50x slower. Both look
# like success to anything that only checks whether the API answers.
out = run(S.check_comfyui())
check("a healthy install is reported as ready", "Ready to build" in out, out[-200:])

BOX["ckpts"] = []
out = run(S.check_comfyui())
check("no weights at all is called out — nothing can run without them",
      "NO MODEL WEIGHTS" in out, out[-300:])
check("and it says to ask before pulling gigabytes the user didn't choose",
      "Ask the user" in out, out[-300:])
BOX["ckpts"] = ["sd15.safetensors"]

BOX["device_type"] = "cpu"
out = run(S.check_comfyui())
check("torch on the CPU is called out — it works, 50x slower, and looks fine",
      "TORCH IS ON THE CPU" in out, out[-300:])
BOX["device_type"] = "cuda"

BOX["manager"] = False
out = run(S.check_comfyui())
check("a missing Manager comes with the commands, not a shrug",
      "git clone https://github.com/Comfy-Org/ComfyUI-Manager" in out, out[-400:])
check("and notes restart_comfyui can't fix it — it IS a Manager route",
      "restart_comfyui is itself a Manager route" in out, out[-400:])
BOX["manager"] = True

# --- what happens when there is no ComfyUI at all ------------------------- #
#
# The agent calling this has a shell; the server does not. So "not reachable"
# has to come back as instructions it can act on, and it has to differ by
# situation — starting an install that exists, creating one that doesn't, and
# NOT installing anything locally when the URL points at another machine (which
# would leave a second, unused ComfyUI on the wrong box).
import pathlib  # noqa: E402
import tempfile  # noqa: E402

DEAD = "http://127.0.0.1:9"
real_url = S.COMFY_URL

fake = pathlib.Path(tempfile.mkdtemp(prefix="fake-comfyui-")) / "ComfyUI"
(fake / "comfy").mkdir(parents=True)
(fake / "main.py").write_text("")
(fake / ".venv" / "bin").mkdir(parents=True)
(fake / ".venv" / "bin" / "python").write_text("")

S.COMFY_URL = DEAD
os.environ["COMFYUI_PATH"] = str(fake)
out = run(S.check_comfyui())
check("an installed-but-stopped ComfyUI is reported as stopped, not missing",
      "isn't running" in out and str(fake) in out, out[:200])
check("and the start command uses the install's own venv python",
      f"{fake}/.venv/bin/python main.py" in out, out[:300])
check("and says to background it — the launch never returns",
      "BACKGROUND" in out, out[:300])

os.environ["COMFYUI_PATH"] = str(fake.parent / "nope")
out = run(S.check_comfyui())
check("with nothing installed, it hands over install commands",
      "git clone https://github.com/comfyanonymous/ComfyUI" in out, out[:200])
check("and answers the Python question instead of leaving it open",
      "Python is NOT a prerequisite" in out and sys.executable in out, out[:400])
check("and says Manager is what the install/restart tools need",
      "ComfyUI-Manager" in out, out[:400])

S.COMFY_URL = "http://some-other-box.local:8188"
out = run(S.check_comfyui())
check("a REMOTE url does not get local install instructions",
      "git clone" not in out and "REMOTE" in out, out[:200])
check("it offers the tunnel instead", "ssh -N -L 8188:127.0.0.1:8188" in out, out[:300])

# The other 40 tools must not just raise a bare ConnectError at an agent that
# skipped step 0 — the advice is attached at the transport, so every one of them
# gets it.
S.COMFY_URL = DEAD
try:
    run(S.list_nodes("ksampler"))
    reached = True
except Exception as exc:  # noqa: BLE001
    reached = False
    check("a tool other than check_comfyui also explains what to do",
          "NOT reachable" in str(exc) and "git clone" in str(exc), str(exc)[:200])
if reached:
    check("a tool other than check_comfyui also explains what to do", False, "no error raised")

check("the advice is not printed twice", run(S.check_comfyui()).count("No ComfyUI found") == 1)
S.COMFY_URL = real_url
os.environ.pop("COMFYUI_PATH", None)

# The bootstrap recipe has to be about THIS machine, or it is just a README.
recipe = S.comfy_install()
check("comfy_install names the interpreter to build the venv from", sys.executable in recipe)
check("comfy_install says Python is already solved",
      "do NOT need to install Python" in recipe, recipe[:200])
check("comfy_install picks a torch story for this box's accelerator",
      any(k in recipe for k in ("NVIDIA detected", "ROCm", "Apple silicon", "No GPU detected")),
      recipe[:400])
check("comfy_install sets up Manager, since half the EXTEND tools need it",
      "ComfyUI-Manager" in recipe)
check("comfy_install warns about where models land",
      "extra_model_paths" in recipe, recipe[-800:])

srv.shutdown()
LOOP.close()
print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all http tool tests passed")
