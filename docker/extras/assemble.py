#!/usr/bin/env python3
"""Worker entrypoint for images with a weight file split across layers: join it, then start.

GHCR caps one layer at ~10 GB, so a larger file (the 14B fp8 Wan models, ~16.4 GB) is baked as
N `<path>.kdp-part<i>` layers by fetch.py --parts, after its full sha256 was verified at build
time. Here, before /start.sh launches ComfyUI, each such file is joined back into <path> in the
container's writable layer (container disk: the parts stay in the read-only image, so the
template needs the file's size on top of the usual scratch space). Each part's size is checked;
KDP_VERIFY_SHA256=1 also re-hashes the joined file (~1 min per 16 GB) against the build value.
Layers are content-addressed by the registry, so the size check is the default.

    python3 /kdp-assemble.py [command ...]   # default command: /start.sh
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SPLIT_DIR = Path(os.environ.get("KDP_SPLIT_DIR", "/kdp-split"))
CHUNK = 64 << 20
HEADROOM = 1 << 30   # leave 1 GB free for ComfyUI's temp/output files


class AssembleError(RuntimeError):
    pass


def entries(split_dir: Path | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for manifest in sorted((split_dir or SPLIT_DIR).glob("*.json")):
        out.extend(f for f in json.loads(manifest.read_text())["files"] if f.get("parts"))
    return out


def _copy(src: Path, dst: int, size: int) -> None:
    """Append `size` bytes of src to the fd `dst` (unbuffered, so both copy paths share the
    kernel file offsets)."""
    fast = hasattr(os, "copy_file_range")
    with open(src, "rb", buffering=0) as fh:
        left = size
        while left:
            n = 0
            if fast:
                try:   # in-kernel copy where the filesystem allows it
                    n = os.copy_file_range(fh.fileno(), dst, min(CHUNK, left))
                except OSError:
                    fast = False
            if n == 0:
                chunk = fh.read(min(CHUNK, left))
                if not chunk:
                    raise AssembleError(f"{src}: {left} bytes short")
                n = os.write(dst, chunk)
                if n != len(chunk):
                    raise AssembleError(f"short write to {src} target")
            left -= n


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def assemble(entry: dict[str, Any], verify: bool = False) -> str:
    dest = Path(entry["path"])
    size = int(entry["size"])
    if dest.exists() and dest.stat().st_size == size:
        return f"ready {dest}"
    parts = [(Path(p["path"]), int(p["size"])) for p in entry["parts"]]
    if sum(s for _, s in parts) != size:
        raise AssembleError(f"{dest}: parts add up to {sum(s for _, s in parts)}, not {size}")
    for p, s in parts:
        if not p.is_file() or p.stat().st_size != s:
            raise AssembleError(f"{p}: missing or not {s} bytes")
    free = shutil.disk_usage(dest.parent).free
    if free < size + HEADROOM:
        raise AssembleError(f"{dest}: needs {(size + HEADROOM) / 1e9:.1f} GB free on the container "
                            f"disk, have {free / 1e9:.1f} GB; raise the template's container disk")
    tmp = dest.with_name(dest.name + ".kdp-tmp")
    t0 = time.monotonic()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        for p, s in parts:
            _copy(p, fd, s)
    finally:
        os.close(fd)
    if tmp.stat().st_size != size:
        tmp.unlink()
        raise AssembleError(f"{dest}: joined {tmp.stat().st_size} bytes, want {size}")
    if verify and (got := sha256_of(tmp)) != entry["sha256"]:
        tmp.unlink()
        raise AssembleError(f"{dest}: sha256 {got} != {entry['sha256']}")
    os.replace(tmp, dest)
    for p, _ in parts:   # hide the parts (a whiteout; the image layer keeps them)
        p.unlink(missing_ok=True)
    return f"joined {dest} ({size / 1e9:.2f} GB, {len(parts)} parts, {time.monotonic() - t0:.0f}s)"


def main(argv: list[str] | None = None) -> int:
    cmd = (sys.argv[1:] if argv is None else argv) or ["/start.sh"]
    verify = os.environ.get("KDP_VERIFY_SHA256") == "1"
    try:
        for entry in entries():
            print(f"kdp-assemble: {assemble(entry, verify)}", flush=True)
    except (AssembleError, OSError, ValueError, KeyError) as e:
        print(f"kdp-assemble FAILED: {e}", file=sys.stderr, flush=True)
        return 1
    os.execv(cmd[0], cmd)
    return 0   # not reached


if __name__ == "__main__":
    sys.exit(main())
