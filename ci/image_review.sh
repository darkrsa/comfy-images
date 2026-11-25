#!/usr/bin/env bash
# CI only: describe what an image adds on top of its base and prepare it for a secret scan.
#   image_review.sh <image> <base> <family> <outdir>
# Writes <outdir>/image-manifest-<family>.txt (history, config, layer sizes, files added or
# changed vs the base, the in-image /kdp-models.json) and <outdir>/scan/ (every added
# non-weight file, the JSON header of every added .safetensors, history and config) for gitleaks.
set -Eeuo pipefail

image="$1" base="$2" family="$3"
mkdir -p "$4"
out="$(cd "$4" && pwd)"   # docker -v needs an absolute host path
manifest="$out/image-manifest-$family.txt"
scan="$out/scan"
mkdir -p "$scan"

list_files() {  # size mtime path, one per file (no hashing: the weights are ~20 GB)
  docker run --rm --network none --entrypoint find "$1" / -xdev \
    \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f -printf '%s %T@ %p\n' \
    | LC_ALL=C sort
}

list_files "$base" >"$out/base-files.txt"
list_files "$image" >"$out/image-files.txt"
# Added or changed (size/mtime differ) on top of the base.
LC_ALL=C comm -13 "$out/base-files.txt" "$out/image-files.txt" >"$out/added-files.txt"

{
  echo "# image: $image"
  echo "# base:  $base"
  echo "# built: $(date -u +%FT%TZ)"
  echo
  echo "## docker history --no-trunc"
  docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' "$image"
  echo
  echo "## image config (env, labels, entrypoint, cmd)"
  docker image inspect --format '{{json .Config}}' "$image"
  echo
  echo "## size"
  docker image inspect --format 'uncompressed {{.Size}} bytes' "$image"
  docker image inspect --format 'base uncompressed {{.Size}} bytes' "$base"
  echo
  echo "## files added or changed vs base (size bytes, mtime, path)"
  cat "$out/added-files.txt"
  echo
  echo "## /kdp-models.json"
  docker run --rm --network none --entrypoint cat "$image" /kdp-models.json
  echo
  echo "## /kdp-tools.json (pinned tool archives, size + sha256 verified at build)"
  docker run --rm --network none --entrypoint sh "$image" -c 'cat /kdp-tools.json 2>/dev/null || echo none'
  echo
  echo "## /kdp-split/*.json (files baked as several layers, joined at worker start)"
  docker run --rm --network none --entrypoint sh "$image" -c 'cat /kdp-split/*.json 2>/dev/null || echo none'
} >"$manifest"

docker history --no-trunc "$image" >"$scan/history.txt"
docker image inspect "$image" >"$scan/inspect.json"
# Copy every added file into scan/, except weights: for those only their JSON header (the only
# free-text part of a .safetensors file) is extracted. A split weight's part 0 starts with that
# header; its other parts are raw tensor bytes and are only listed. Files unpacked from a pinned
# tool archive (/kdp-tools.json: upstream bytes, sha256-checked at build) are only listed too.
awk '{print $3}' "$out/added-files.txt" >"$scan/paths.txt"
docker run --rm --network none -v "$scan:/scan" --entrypoint python3 "$image" -c '
import json, shutil, struct
from pathlib import Path
tools = Path("/kdp-tools.json")
tool_dirs = [t["path"].rstrip("/") + "/" for t in json.loads(tools.read_text())] if tools.exists() else []
for line in Path("/scan/paths.txt").read_text().split():
    src, dst = Path(line), Path("/scan/files" + line)
    if any(line.startswith(d) for d in tool_dirs):
        continue
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.startswith(".kdp-part") and src.suffix != ".kdp-part0":
        continue
    if src.suffix in (".safetensors", ".kdp-part0"):
        with open(src, "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            header = json.loads(fh.read(n))
        dst.with_name(dst.name + ".header.json").write_text(json.dumps(header.get("__metadata__", {}), indent=1)
            + "\n" + "\n".join(sorted(k for k in header if k != "__metadata__")) + "\n")
    else:
        shutil.copy(src, dst)
'
echo "review files: $(find "$scan" -type f | wc -l), manifest: $manifest"
