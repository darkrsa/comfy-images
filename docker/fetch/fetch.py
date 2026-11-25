#!/usr/bin/env python3
"""Download one model file into a kdp-comfy image at build time, verified.

    python3 fetch.py --root /comfyui <path> <url> <sha256|-> <licence_repo> <licence>

The same checks as kdp-studio infra/runpod-serverless/provision.sh (which fills the optional network volume), ported to the
standard library because runpod/worker-comfyui has python3 but no curl:
  1. refuse unless the upstream Hugging Face card (`licence_repo`) reports `licence`, the licence
     models.yaml declares for the file (images.py only renders licences the owner approved there);
  2. read the exact size + sha256 from HF's LFS headers (x-linked-size / x-linked-etag) on the
     un-followed resolve redirect; a pinned sha256 (from models.yaml) must match them;
  3. download in parallel 64 MB ranges (each resumed + retried) to <root>/<path>.part, check
     size and sha256, move in place;
  4. record the file in /kdp-models.json (what the image adds, for review) and print
     `verified <path> <size> <sha256>` (CI pins these values into models.yaml);
  5. with --parts N (a file over GHCR's 10 GB layer cap), split the verified file into N
     `<path>.kdp-part<i>` files, each COPYed into the image as its own layer and joined back at
     worker start by docker/extras/assemble.py; the manifest entry lists the parts.
Public URLs only: no token is read, so none can end up in a layer.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
MANIFEST = Path(os.environ.get("KDP_MANIFEST", "/kdp-models.json"))
CHUNK = 8 << 20
RANGE = 64 << 20          # bytes per ranged request
PARTS = int(os.environ.get("KDP_FETCH_PARTS", "16"))  # concurrent ranges
UA = {"User-Agent": "kdp-comfy-image-build"}


class FetchError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # surface the 302 and its LFS headers
        return None


def licence_of(repo: str) -> str:
    """The card's `license`; for `license: other`, its `license_name` (e.g. dinov3-license),
    so a custom licence is matched by name, not accepted as "other"."""
    with urllib.request.urlopen(urllib.request.Request(f"{HF}/api/models/{repo}", headers=UA),
                                timeout=60) as r:
        info = json.load(r)
    card = info.get("cardData") or {}
    lic = card.get("license")
    if lic == "other" and card.get("license_name"):
        lic = card["license_name"]
    if not lic:
        lic = next((t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")),
                   "unknown")
    return str(lic)


def remote_meta(url: str, max_hops: int = 5) -> tuple[int, str]:
    """HEAD without following the redirect to the CDN. HF first answers with same-host
    redirects (e.g. /api/resolve-cache/...); follow those, like huggingface_hub does, until a
    hop carries the LFS headers."""
    opener = urllib.request.build_opener(_NoRedirect)
    seen = []
    for _ in range(max_hops):
        try:
            resp = opener.open(urllib.request.Request(url, method="HEAD", headers=UA), timeout=60)
            code, headers = resp.status, resp.headers
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise FetchError(f"HEAD {url}: HTTP {e.code} (gated or moved?)") from None
            code, headers = e.code, e.headers
        size, etag = headers.get("x-linked-size"), (headers.get("x-linked-etag") or "").strip('"')
        if size and len(etag) == 64:
            return int(size), etag
        seen.append(f"{code} {url}")
        location = headers.get("Location") or ""
        if not location or urllib.parse.urlsplit(location).netloc not in ("", urllib.parse.urlsplit(url).netloc):
            break  # final answer, or a cross-host (CDN) redirect without LFS headers
        url = urllib.parse.urljoin(url, location)
    raise FetchError("no LFS size/sha256 headers (not an LFS file, or moved?); hops: " + " -> ".join(seen))


def final_url(url: str) -> str:
    """The signed CDN URL HF redirects to (valid for a while; re-resolved when it expires),
    so the ranged requests don't each hit HF's rate-limited resolve endpoint."""
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.geturl()


def _get_range(target: list[str], url: str, part: Path, start: int, end: int,
               attempts: int) -> int:
    """Fetch bytes start..end (inclusive) of target[0] into `part` at that offset, resuming
    within the range after a cut connection. Returns the byte count."""
    pos = start
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(target[0], headers={**UA, "Range": f"bytes={pos}-{end}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.status != 206:
                    raise FetchError(f"server ignored Range (HTTP {r.status})")
                with open(part, "r+b") as fh:
                    fh.seek(pos)
                    while pos <= end and (chunk := r.read(min(CHUNK, end + 1 - pos))):
                        fh.write(chunk)
                        pos += len(chunk)
            if pos == end + 1:
                return end + 1 - start
        except urllib.error.HTTPError as e:
            print(f"  bytes {start}-{end}: attempt {attempt}/{attempts}: {e}", file=sys.stderr, flush=True)
            if e.code in (401, 403, 410):   # signed URL expired: resolve it again
                target[0] = final_url(url)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            print(f"  bytes {start}-{end}: attempt {attempt}/{attempts}: {e}", file=sys.stderr, flush=True)
        time.sleep(min(30, 2 ** attempt))
    raise FetchError(f"bytes {start}-{end} incomplete after {attempts} attempts (at {pos})")


def download(url: str, part: Path, size: int, attempts: int = 8) -> None:
    """Parallel ranged download: a single HF/Xet stream runs at a few MB/s and gets cut,
    so PARTS workers each fetch RANGE-sized slices into a preallocated file."""
    with open(part, "wb") as fh:
        fh.truncate(size)
    ranges = [(a, min(a + RANGE, size) - 1) for a in range(0, size, RANGE)]
    target = [final_url(url)]
    done = 0
    with ThreadPoolExecutor(PARTS) as pool:
        futures = [pool.submit(_get_range, target, url, part, a, b, attempts) for a, b in ranges]
        for i, fut in enumerate(as_completed(futures), 1):
            done += fut.result()   # re-raises a range's FetchError
            if i % max(1, len(ranges) // 10) == 0 or i == len(ranges):
                print(f"  {done / 1e9:.2f}/{size / 1e9:.2f} GB", flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def record(entry: dict) -> None:
    files = json.loads(MANIFEST.read_text())["files"] if MANIFEST.exists() else []
    files = [f for f in files if f["path"] != entry["path"]] + [entry]
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": files}, indent=2) + "\n")
    os.replace(tmp, MANIFEST)


PART_SUFFIX = ".kdp-part"


def split(dest: Path, n: int) -> list[dict[str, Any]]:
    """Cut `dest` into n near-equal `<dest>.kdp-part<i>` files (sizes differ by at most one
    byte), each with its own sha256. In place, from the tail: each part is copied out and `dest`
    truncated, so the build disk holds the file plus one part at most; `dest` becomes part 0."""
    size = dest.stat().st_size
    if n < 2 or size < n:
        raise FetchError(f"cannot split {size} bytes into {n} parts")
    base, extra = divmod(size, n)
    starts = [i * base + min(i, extra) for i in range(n)] + [size]
    parts: list[dict[str, Any]] = [{}] * n
    for i in reversed(range(n)):
        out = dest.with_name(f"{dest.name}{PART_SUFFIX}{i}")
        start, want = starts[i], starts[i + 1] - starts[i]
        h = hashlib.sha256()
        if i:
            with open(dest, "r+b") as src, open(out, "wb") as fh:
                src.seek(start)
                left = want
                while left and (chunk := src.read(min(CHUNK, left))):
                    fh.write(chunk)
                    h.update(chunk)
                    left -= len(chunk)
                if left:
                    raise FetchError(f"{dest}: short read while splitting")
                src.truncate(start)
        else:
            os.replace(dest, out)
            h = hashlib.sha256()
            with open(out, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    h.update(chunk)
        parts[i] = {"path": str(out), "size": want, "sha256": h.hexdigest()}
    return parts


def fetch(root: Path, path: str, url: str, want_sha: str, repo: str, want_licence: str,
          parts: int = 1) -> dict:
    want_sha = "" if want_sha == "-" else want_sha.lower()
    lic = licence_of(repo)
    if lic != want_licence:
        raise FetchError(f"upstream {repo} licence is {lic!r}, expected {want_licence!r}")
    size, lfs_sha = remote_meta(url)
    if want_sha and want_sha != lfs_sha:
        raise FetchError(f"remote sha256 {lfs_sha} != pinned {want_sha}")
    dest = root / path
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"download {path} ({size / 1e9:.2f} GB) from {url}", flush=True)
    t0 = time.monotonic()
    download(url, part, size)
    got = sha256_of(part)
    if got != lfs_sha:
        part.unlink()
        raise FetchError(f"sha256 {got} != expected {lfs_sha} (deleted)")
    os.replace(part, dest)
    entry: dict[str, Any] = {"path": str(dest), "url": url, "size": size, "sha256": got,
             "licence": lic, "licence_repo": repo}
    print(f"verified {path} {size} {got} ({time.monotonic() - t0:.0f}s)", flush=True)
    if parts > 1:
        entry["parts"] = split(dest, parts)
        for p in entry["parts"]:
            print(f"  part {p['path']} {p['size']} {p['sha256']}", flush=True)
    record(entry)
    return entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--parts", type=int, default=1,
                    help="split the verified file into this many layer-sized parts")
    ap.add_argument("path")
    ap.add_argument("url")
    ap.add_argument("sha256", help="pinned sha256, or - to take HF's LFS sha256")
    ap.add_argument("licence_repo")
    ap.add_argument("licence", help="licence the upstream card must report (from models.yaml)")
    a = ap.parse_args(argv)
    try:
        fetch(a.root, a.path, a.url, a.sha256, a.licence_repo, a.licence, a.parts)
    except (FetchError, urllib.error.URLError, http.client.HTTPException, OSError) as e:
        print(f"fetch FAILED {a.path}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
