"""Run one Wan2.2 LoRA training on a RunPod GPU pod, under a dollar cap, and tear it down by ID.

    RUNPOD_API_KEY=... BUCKET_...=... python run_pod.py --dataset zaffy_v1.zip --kit-ref <commit> \\
        --out out/zaffy_v1 --max-usd 4 [--steps 2000 --rank 32 --lr 1e-4]

1. uploads the dataset zip to R2 datasets/<run_id>.zip (credentials from the BUCKET_* env);
2. creates a 24 GB pod (RTX 4090 -> RTX 3090 -> RTX A5000; never larger) running
   ostris/aitoolkit:0.13.43 with pod_train.sh from comfy-images at --kit-ref; R2 credentials reach
   the pod only as RunPod secret references {{ RUNPOD_SECRET_r2_* }};
3. records the pod ID in <out>/created.json before anything else, polls runs/<run_id>/status.json;
4. stops when the stage is done/failed, the estimate (uptime x costPerHr) would pass --max-usd,
   or --max-hours passes; downloads runs/<run_id>/ to <out>; terminates the pod by ID in `finally`
   and checks it is gone. `--teardown <out>/created.json` repeats the teardown.
RunPod Billing is the source of truth; the printed cost is an estimate.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

GQL = "https://api.runpod.io/graphql"
IMAGE = "ostris/aitoolkit:0.13.43@sha256:ba3327cebd5aa9ad3f539c52a1c4fa30831d3365d12844deda9c9a14f61411c6"
GPUS = ("NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000")   # 24 GB only
SECRETS = {"BUCKET_ENDPOINT_URL": "r2_endpoint_url", "BUCKET_NAME": "r2_bucket",
           "BUCKET_ACCESS_KEY_ID": "r2_access_key_id", "BUCKET_SECRET_ACCESS_KEY": "r2_secret_access_key"}
HERE = Path(__file__).resolve().parent


def gql(query: str, variables: dict | None = None) -> dict:
    key = os.environ["RUNPOD_API_KEY"]
    req = urllib.request.Request(GQL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "comfy-images-train"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"])[:500])
    return body["data"]


def r2(*args: str) -> str:
    return subprocess.run([sys.executable, str(HERE / "r2.py"), *args], check=True,
                          capture_output=True, text=True).stdout


def create_pod(run_id: str, args: argparse.Namespace) -> dict:
    env = [{"key": k, "value": "{{ RUNPOD_SECRET_%s }}" % v} for k, v in SECRETS.items()]
    env += [{"key": k, "value": str(v)} for k, v in
            {"RUN_ID": run_id, "KIT_REF": args.kit_ref, "STEPS": args.steps, "RANK": args.rank, "LR": args.lr,
              "ART_REPEATS": args.art_repeats, "RENDER_REPEATS": args.render_repeats}.items()]
    url = f"https://raw.githubusercontent.com/darkrsa/comfy-images/{args.kit_ref}/training/wan22-lora/pod_train.sh"
    cmd = f"bash -c 'curl -fsSL {url} -o /pod_train.sh && bash /pod_train.sh; sleep infinity'"
    last = None
    for gpu in GPUS:
        q = """mutation($i: PodFindAndDeployOnDemandInput) { podFindAndDeployOnDemand(input: $i) {
                 id costPerHr machineId gpuCount } }"""
        i = {"cloudType": "ALL", "gpuCount": 1, "gpuTypeId": gpu, "name": f"zaffy-lora-{run_id}",
             "imageName": IMAGE, "containerDiskInGb": 160, "volumeInGb": 0, "minMemoryInGb": 48,
             "minVcpuCount": 8, "dockerArgs": cmd, "env": env, "ports": "",
             "allowedCudaVersions": ["13.0"]}   # the image is CUDA 13.0 runtime: host driver must support it
        try:
            try:
                pod = gql(q, {"i": i})["podFindAndDeployOnDemand"]
            except RuntimeError as exc:   # field unknown to this API version: validation error, nothing created
                if "allowedCudaVersions" not in str(exc):
                    raise
                print("warning: API has no allowedCudaVersions; host driver not filtered", flush=True)
                i.pop("allowedCudaVersions")
                pod = gql(q, {"i": i})["podFindAndDeployOnDemand"]
            if pod and pod.get("id"):
                return {**pod, "gpu": gpu}
        except RuntimeError as exc:   # no stock for this GPU: try the next one
            last = exc
            print(f"{gpu}: {exc}", flush=True)
    raise RuntimeError(f"no 24 GB pod available: {last}")


def teardown(state_file: Path) -> None:
    state = json.loads(state_file.read_text())
    pid = state.get("pod", {}).get("id")
    if not pid:
        return
    gql("mutation($p: String!) { podTerminate(input: {podId: $p}) }", {"p": pid})
    gone = gql("query($p: String!) { pod(input: {podId: $p}) { id } }", {"p": pid})["pod"] is None
    state["terminated"] = gone
    state_file.write_text(json.dumps(state, indent=2))
    print(f"pod {pid} terminated: {gone}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset"); ap.add_argument("--kit-ref"); ap.add_argument("--out", required=True)
    ap.add_argument("--max-usd", type=float, default=4.0); ap.add_argument("--max-hours", type=float, default=6.0)
    ap.add_argument("--steps", type=int, default=2000); ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--art-repeats", type=int, default=2); ap.add_argument("--render-repeats", type=int, default=1)
    ap.add_argument("--lr", default="1e-4"); ap.add_argument("--teardown", metavar="CREATED_JSON")
    args = ap.parse_args(argv)
    if args.teardown:
        teardown(Path(args.teardown)); return 0
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True); state_file = out / "created.json"
    run_id = time.strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:6]
    r2("put", args.dataset, f"datasets/{run_id}.zip")
    state: dict = {"run_id": run_id, "started": time.time()}
    state_file.write_text(json.dumps(state, indent=2))
    rc = 1
    try:
        state["pod"] = create_pod(run_id, args)
        state_file.write_text(json.dumps(state, indent=2))
        rate = float(state["pod"]["costPerHr"]); t0 = time.time(); stage = ""
        print(f"run {run_id}: pod {state['pod']['id']} {state['pod']['gpu']} ${rate}/h, cap ${args.max_usd}", flush=True)
        while True:
            time.sleep(60)
            hours = (time.time() - t0) / 3600; est = hours * rate
            s = json.loads(r2("read-status", run_id) or "{}")
            if s.get("stage") != stage:
                stage = s.get("stage", ""); print(f"{hours*60:5.0f} min  ${est:.2f}  stage={stage} {s.get('note','')}", flush=True)
            if stage in ("done", "failed"):
                rc = 0 if stage == "done" else 1; break
            if est + rate / 60 > args.max_usd or hours > args.max_hours:
                print(f"stopping: estimate ${est:.2f} / {hours:.1f} h reached the cap", flush=True); break
        state["estimate_usd"] = round((time.time() - t0) / 3600 * rate, 3)
    finally:
        try:
            r2("get-dir", run_id, str(out / "result"))
        except Exception as exc:   # keep going to teardown
            print(f"download failed: {exc}", flush=True)
        state_file.write_text(json.dumps(state, indent=2))
        teardown(state_file)
        print(f"estimate ${state.get('estimate_usd')}; outputs in {out/'result'}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
