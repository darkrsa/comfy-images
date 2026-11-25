"""Tiny R2 (S3) helper shared by the pod and the operator's machine. Credentials only from the
environment: BUCKET_ENDPOINT_URL, BUCKET_NAME, BUCKET_ACCESS_KEY_ID, BUCKET_SECRET_ACCESS_KEY.

    r2.py put FILE KEY            r2.py get KEY FILE
    r2.py put-dir RUN_ID DIR      (uploads DIR/** to runs/RUN_ID/, skipping optimizer states)
    r2.py get-dir RUN_ID DIR      r2.py status RUN_ID STAGE [NOTE]      r2.py read-status RUN_ID
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def client():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=os.environ["BUCKET_ENDPOINT_URL"], region_name="auto",
                        aws_access_key_id=os.environ["BUCKET_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["BUCKET_SECRET_ACCESS_KEY"],
                        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}))


BUCKET = os.environ.get("BUCKET_NAME", "")
SKIP = (".pt", "optimizer")   # optimizer states are large and not needed after the run


def put_dir(run_id: str, d: str) -> None:
    c = client()
    for p in sorted(Path(d).rglob("*")):
        if p.is_file() and not any(s in p.name for s in SKIP):
            c.upload_file(str(p), BUCKET, f"runs/{run_id}/{p.relative_to(d).as_posix()}")


def get_dir(run_id: str, d: str) -> None:
    c = client(); prefix = f"runs/{run_id}/"
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            dst = Path(d) / o["Key"][len(prefix):]; dst.parent.mkdir(parents=True, exist_ok=True)
            c.download_file(BUCKET, o["Key"], str(dst))


def main(a: list[str]) -> int:
    cmd = a[0]
    if cmd == "put":
        client().upload_file(a[1], BUCKET, a[2])
    elif cmd == "get":
        Path(a[2]).parent.mkdir(parents=True, exist_ok=True); client().download_file(BUCKET, a[1], a[2])
    elif cmd == "put-dir":
        put_dir(a[1], a[2])
    elif cmd == "get-dir":
        get_dir(a[1], a[2])
    elif cmd == "status":
        body = {"stage": a[2], "note": a[3] if len(a) > 3 else "", "time": time.time()}
        client().put_object(Bucket=BUCKET, Key=f"runs/{a[1]}/status.json", Body=json.dumps(body).encode())
    elif cmd == "read-status":
        try:
            print(client().get_object(Bucket=BUCKET, Key=f"runs/{a[1]}/status.json")["Body"].read().decode())
        except Exception:   # not written yet
            print("{}")
    else:
        print(__doc__); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
