"""Smoke-test one image on RunPod serverless: create a template + endpoint, run one workflow,
save the outputs, delete what it created by ID, and report estimate vs actual cost.

    RUNPOD_API_KEY=... python runpod_smoke.py \\
        --image ghcr.io/darkrsa/kdp-comfy-trellis2:20261124-abc1234 \\
        --workflow workflows/trellis2_image_to_glb.json --input zaffy.png=path/to/front.png \\
        --out out/trellis2 --max-usd 1.0

Uses only api.runpod.io (REST /v2) and api.runpod.ai (job API). Every resource it creates is
recorded in <out>/created.json before it is used, and torn down by ID in `finally`, even after a
failure or Ctrl-C; `--teardown <out>/created.json` repeats the teardown. The job is cancelled when
its billed time (`executionTime` + `delayTime`) x the GPU pool's serverless rate would pass
--max-usd. RunPod Billing is the source of truth; the printed cost is an estimate from that formula.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

API = "https://api.runpod.io/v2"
JOBS = "https://api.runpod.ai/v2"
HTTP_TIMEOUT_S = 120
ALLOWED_POOLS = ("AMPERE_24", "ADA_24")   # project rule: 24 GB pools only, never escalate
# --r2: the handler uploads every output to Cloudflare R2. Values come from RunPod secrets with these
# names (RunPod console -> Secrets), never from this script or its arguments.
R2_SECRETS = {"BUCKET_ENDPOINT_URL": "r2_endpoint_url", "BUCKET_NAME": "r2_bucket",
              "BUCKET_ACCESS_KEY_ID": "r2_access_key_id", "BUCKET_SECRET_ACCESS_KEY": "r2_secret_access_key"}


class SmokeError(RuntimeError):
    """A RunPod call failed or the job did not finish as expected."""


def _call(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        raise SmokeError("RUNPOD_API_KEY is not set")
    req = urllib.request.Request(
        url, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "comfy-images-smoke"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            text = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise SmokeError(f"{method} {url}: HTTP {exc.code} {exc.read().decode()[:500]}") from None
    return json.loads(text) if text.strip() else None


def serverless_rate_per_s(pool: str) -> float:
    """Highest serverless $/h among the GPUs in `pool`, per second (worst case)."""
    catalog = _call("GET", f"{API}/catalog/gpus")
    gpus = catalog.get("gpus", []) if isinstance(catalog, dict) else catalog
    prices = [g["price"]["serverless"] for g in gpus
              if g.get("pool") == pool and (g.get("price") or {}).get("serverless")]
    if not prices:
        raise SmokeError(f"no serverless price for pool {pool}")
    return float(max(prices)) / 3600


def record(state_file: Path, state: dict[str, Any]) -> None:
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, state_file)


def teardown(state: dict[str, Any]) -> list[str]:
    """Delete the endpoint, then the template, by the IDs this run recorded. Returns a log."""
    log = []
    for kind, path in (("endpoint", "serverless"), ("template", "templates")):
        resource_id = state.get(f"{kind}_id")
        if not resource_id or state.get(f"{kind}_deleted"):
            continue
        for attempt in range(1, 6):   # an endpoint with a worker still stopping can refuse at first
            try:
                _call("DELETE", f"{API}/{path}/{resource_id}")
                state[f"{kind}_deleted"] = True
                log.append(f"deleted {kind} {resource_id}")
                break
            except SmokeError as exc:
                log.append(f"delete {kind} {resource_id} attempt {attempt}: {exc}")
                time.sleep(10 * attempt)
    return log


def run(args: argparse.Namespace) -> int:
    if args.gpu not in ALLOWED_POOLS:
        raise SmokeError(f"--gpu must be one of {ALLOWED_POOLS}")
    if ":latest" in args.image or ":" not in args.image.rsplit("/", 1)[-1]:
        raise SmokeError("--image must be a dated tag, not :latest")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / "created.json"
    workflow = json.loads(Path(args.workflow).read_text(encoding="utf-8"))
    images = []
    for spec in args.input:
        name, _, path = spec.partition("=")
        images.append({"name": name, "image": base64.b64encode(Path(path).read_bytes()).decode()})
    rate = serverless_rate_per_s(args.gpu)
    tag = uuid.uuid4().hex[:6]
    state: dict[str, Any] = {"image": args.image, "gpu": args.gpu, "rate_usd_per_s": rate}
    try:
        template = _call("POST", f"{API}/templates", {
            "name": f"smoke-{tag}", "image": args.image, "disk": 20, "serverless": True,
            "env": {"MAX_RESULT_MB": str(args.max_result_mb),
                    **({k: "{{ RUNPOD_SECRET_%s }}" % v for k, v in R2_SECRETS.items()} if args.r2 else {})}})
        state["template_id"] = template["id"]
        record(state_file, state)
        endpoint = _call("POST", f"{API}/serverless", {
            "name": f"smoke-{tag}", "templateId": template["id"], "type": "QUEUE",
            "gpu": {"pools": [args.gpu], "count": 1},
            "workers": {"min": 0, "max": 1, "idleTimeout": 5},
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 4}, "flashboot": "OFF"})
        state["endpoint_id"] = endpoint["id"]
        record(state_file, state)
        print(f"template {template['id']}, endpoint {endpoint['id']} ({args.gpu}, "
              f"${rate * 3600:.2f}/h, cap ${args.max_usd:.2f})", flush=True)
        job = _call("POST", f"{JOBS}/{endpoint['id']}/run", {"input": {"workflow": workflow, "images": images}})
        state["job_id"] = job["id"]
        record(state_file, state)
        started, status = time.monotonic(), {}
        while time.monotonic() - started < args.timeout_s:
            status = _call("GET", f"{JOBS}/{endpoint['id']}/status/{job['id']}")
            billed_s = ((status.get("executionTime") or 0) + (status.get("delayTime") or 0)) / 1000
            print(f"{time.monotonic() - started:6.0f}s {status.get('status')} billed~{billed_s:.0f}s "
                  f"${billed_s * rate:.3f}", flush=True)
            if status.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                break
            # executionTime only lands at the end; while running, bound wall time after pickup.
            running_s = time.monotonic() - started if status.get("status") == "IN_PROGRESS" else 0
            if max(billed_s, running_s) * rate > args.max_usd:
                _call("POST", f"{JOBS}/{endpoint['id']}/cancel/{job['id']}")
                raise SmokeError(f"cost cap ${args.max_usd} reached; job cancelled")
            time.sleep(args.poll_s)
        state["status"] = status.get("status")
        billed_s = ((status.get("executionTime") or 0) + (status.get("delayTime") or 0)) / 1000
        state["execution_ms"], state["delay_ms"] = status.get("executionTime"), status.get("delayTime")
        state["estimated_usd"] = round(billed_s * rate, 4)
        record(state_file, state)
        output = status.get("output") or {}
        if status.get("status") != "COMPLETED" or "error" in output:
            raise SmokeError(f"job {status.get('status')}: {json.dumps(output or status.get('error'))[:2000]}")
        for warning in output.get("warnings", []):
            print(f"handler warning: {warning}", flush=True)
        state["files"] = []
        for item in output.get("files", []):
            target = out / Path(item["filename"]).name
            if item.get("base64"):
                target.write_bytes(base64.b64decode(item["base64"]))
            elif item.get("url"):
                with urllib.request.urlopen(item["url"], timeout=HTTP_TIMEOUT_S) as resp:
                    target.write_bytes(resp.read())
            else:
                print(f"NOT saved {item['filename']}: no inline data and no link", flush=True)
                continue
            state["files"].append({"file": str(target), "key": item.get("key")})
            print(f"saved {target} ({item['bytes']} bytes, node {item['node']}, {item['kind']}, "
                  f"bucket key {item.get('key')})")
        return 0
    finally:
        for line in teardown(state):
            print(line, flush=True)
        record(state_file, state)
        print(f"estimate: billed {state.get('execution_ms')} ms exec + {state.get('delay_ms')} ms delay "
              f"= ${state.get('estimated_usd')}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image")
    ap.add_argument("--workflow")
    ap.add_argument("--input", action="append", default=[], help="name=path, uploaded as ComfyUI input")
    ap.add_argument("--out", default="out")
    ap.add_argument("--gpu", default="AMPERE_24")
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--max-result-mb", type=float, default=9, help="inline reply cap; /run allows 10 MB")
    ap.add_argument("--r2", action="store_true", help="have the handler upload outputs to R2 (RunPod secrets r2_*)")
    ap.add_argument("--timeout-s", type=float, default=3600)
    ap.add_argument("--poll-s", type=float, default=15)
    ap.add_argument("--teardown", help="created.json of an earlier run: delete what it lists, then exit")
    args = ap.parse_args(argv)
    try:
        if args.teardown:
            state = json.loads(Path(args.teardown).read_text(encoding="utf-8"))
            for line in teardown(state):
                print(line)
            record(Path(args.teardown), state)
            return 0
        if not (args.image and args.workflow):
            ap.error("--image and --workflow are required")
        return run(args)
    except SmokeError as exc:
        print(f"smoke FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
