#!/usr/bin/env python3
"""Install one pinned tool archive (e.g. Blender) into a kdp-comfy image at build time, verified.

    python3 tool.py <name> <version> <urls> <sha256> <size> <dest_dir> <binary>

<urls> is a comma-separated list of https URLs of the same archive (models.yaml `url`, then its
`mirrors`), tried in order. Standard library only (runpod/worker-comfyui has python3 but no curl):
  1. download the archive to a temp file (resumed + retried; an HTTP 4xx refusal moves on to the
     next URL at once), check the exact byte size and sha256 pinned in models.yaml `tools:`; any
     mismatch fails the build, so a mirror can serve nothing but the pinned bytes;
  2. unpack the .tar.xz into <dest_dir> (its single top-level folder stripped), link
     <dest_dir>/<binary> as /usr/local/bin/<binary>, delete the archive;
  3. record the tool in /kdp-tools.json and print `verified-tool <name> <version> <size> <sha256>`.
Public URLs only: no token is read, so none can end up in a layer.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 8 << 20
MANIFEST = Path(os.environ.get("KDP_TOOLS_MANIFEST", "/kdp-tools.json"))
UA = {"User-Agent": "comfy-images-build"}


class ToolError(RuntimeError):
    pass


def download(urls: list[str], dst: Path, size: int, tries: int = 5) -> str:
    """Fetch the archive into dst from the first URL that serves it; return that URL."""
    for url in urls:
        for attempt in range(1, tries + 1):
            have = dst.stat().st_size if dst.exists() else 0
            if have == size:
                return url
            req = urllib.request.Request(url, headers={**UA, **({"Range": f"bytes={have}-"} if have else {})})
            try:
                with urllib.request.urlopen(req, timeout=120) as r, open(dst, "ab" if have else "wb") as fh:
                    if have and r.status != 206:   # server ignored the range: start over
                        fh.truncate(0)
                    while chunk := r.read(CHUNK):
                        fh.write(chunk)
            except urllib.error.HTTPError as e:
                print(f"tool: {url}: attempt {attempt}: {e}", file=sys.stderr, flush=True)
                if 400 <= e.code < 500:   # refused (e.g. 403 to CI runners): next URL
                    break
                time.sleep(min(30, 2 ** attempt))
            except OSError as e:
                print(f"tool: {url}: attempt {attempt}: {e}", file=sys.stderr, flush=True)
                time.sleep(min(30, 2 ** attempt))
        if (dst.stat().st_size if dst.exists() else 0) == size:
            return url
    got = dst.stat().st_size if dst.exists() else 0
    raise ToolError(f"{', '.join(urls)}: got {got} bytes, want {size}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def unpack(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:xz") as tf:
        members = tf.getmembers()
        tops = {m.name.split("/", 1)[0] for m in members}
        if len(tops) != 1:
            raise ToolError(f"{archive}: want one top-level folder, got {sorted(tops)[:5]}")
        for m in members:
            m.name = m.name.split("/", 1)[1] if "/" in m.name else ""
        keep = [m for m in members if m.name]
        if hasattr(tarfile, "tar_filter"):   # Python >= 3.12 (and security backports)
            tf.extractall(dest, members=keep, filter="tar")
        else:
            tf.extractall(dest, members=keep)


def install(name: str, version: str, urls: str, sha256: str, size: int, dest: Path, binary: str) -> dict[str, object]:
    candidates = [u for u in urls.split(",") if u]
    bad = [u for u in candidates if not u.startswith("https://")]
    if bad or not candidates:
        raise ToolError(f"{name}: https URLs only, got {bad or urls!r}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / Path(candidates[0]).name
        url = download(candidates, archive, size)
        got = sha256_of(archive)
        if got != sha256:
            raise ToolError(f"{name}: sha256 {got} != pinned {sha256}")
        unpack(archive, dest)
    exe = dest / binary
    if not exe.is_file():
        raise ToolError(f"{name}: {exe} missing after unpack")
    link = Path("/usr/local/bin") / binary
    link.unlink(missing_ok=True)
    link.symlink_to(exe)
    entry = {"name": name, "version": version, "url": url, "sha256": sha256, "size": size,
             "path": str(dest), "binary": str(link)}
    tools = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    MANIFEST.write_text(json.dumps([t for t in tools if t["name"] != name] + [entry], indent=1) + "\n")
    print(f"verified-tool {name} {version} {size} {sha256}", flush=True)
    print(f"tool: {name} downloaded from {url}", flush=True)
    return entry


def main(argv: list[str] | None = None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if len(a) != 7:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        install(a[0], a[1], a[2], a[3], int(a[4]), Path(a[5]), a[6])
    except (ToolError, OSError, ValueError, tarfile.TarError) as e:
        print(f"tool FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
