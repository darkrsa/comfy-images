"""Render docker/<family>/{Dockerfile,.dockerignore} from models.yaml .

    python images.py render      # rewrite the Dockerfiles
    python images.py check       # exit 1 if a committed Dockerfile is stale (tests + CI)
    python images.py families    # print the family names (CI matrix)
    python images.py extras-families   # families that copy files from docker/extras

Each image = the pinned worker-comfyui base + one layer per weight file: the fetcher
(docker/fetch/fetch.py) is bind-mounted from a separate named build context for the RUN only, so
no script, repo file, build arg or token is ever written into a layer. Two optional additions come
from the `extras` build context (docker/extras): the licence texts a weight's licence requires
(`licence_files`) and, for a family with `handler: outputs`, a handler that returns every output
kind (docker/extras/handler.py replaces the stock /handler.py).

A file over GHCR's ~10 GB layer cap sets `parts: N`: a build stage downloads and verifies the whole
file, then cuts it into N `.kdp-part<i>` files; the image COPYs each part as its own layer, and
docker/extras/assemble.py (the image's CMD) joins them before /start.sh starts ComfyUI.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MODELS_YAML = HERE / "models.yaml"


def load_models(path: Path = MODELS_YAML) -> dict:
    return yaml.safe_load(path.read_text())


DOCKER = HERE / "docker"
EXTRAS = DOCKER / "extras"
REGISTRY = "ghcr.io/darkrsa"
SOURCE = "https://github.com/darkrsa/comfy-images"
# runpod/worker-comfyui 5.10.0-base (ComfyUI 0.34.0), pinned by digest; see REPORT.
BASE_IMAGE = ("runpod/worker-comfyui:5.10.0-base"
              "@sha256:e9d18d3db15839ebb10c10c824109fc2956f31ef7bf21916220c008855b0d538")
COMFY_ROOT = "/comfyui"
# Template container disk = writable scratch space on top of the image, not room for the image:
# worker-comfyui's .runpod/hub.json gives its 24.5 GB (uncompressed) base image 20 GB. The baked
# weights are read-only image layers, so the same 20 GB fits every family.
CONTAINER_DISK_GB = 20
LAYER_CAP_GB = 9.5   # a part must stay under GHCR's ~10 GB layer cap even if it does not compress
DOCKERIGNORE = "# Build context = this directory; only the Dockerfile is sent.\n*\n!Dockerfile\n"
HANDLERS = {"outputs": "handler.py"}
ASSEMBLE = "assemble.py"   # docker/extras file that joins split weights at worker start   # family `handler:` value -> file in docker/extras
# models.yaml licence id -> SPDX expression for the image label.
SPDX = {"apache-2.0": "Apache-2.0", "mit": "MIT", "dinov3-license": "LicenseRef-DINOv3",
        "gpl-2.0-or-later": "GPL-2.0-or-later"}
TOOL_KEYS = {"version", "url", "size", "sha256", "dest", "binary", "licence", "licence_url", "apt"}


class ModelsError(ValueError):
    """models.yaml is inconsistent: unknown or unapproved licence, missing file or tool, bad handler."""


def image_name(family: str) -> str:
    return f"{REGISTRY}/kdp-comfy-{family}"


def validate(models: dict) -> None:
    """Raise ModelsError unless every family's files exist, carry an approved licence with an
    SPDX label, ship their licence texts, and name a known handler."""
    approved = models.get("approved_licences") or {}
    by_name = {f["name"]: f for f in models["files"]}
    tools = models.get("tools") or {}
    for name, t in tools.items():
        if set(t) != TOOL_KEYS:
            raise ModelsError(f"tool {name}: keys {sorted(t)} != {sorted(TOOL_KEYS)}")
        if not str(t["url"]).startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", str(t["sha256"])):
            raise ModelsError(f"tool {name}: needs an https url and a pinned sha256")
        if not isinstance(t["size"], int) or t["size"] <= 0:
            raise ModelsError(f"tool {name}: size must be the archive's exact byte count")
        if t["licence"] not in approved or t["licence"] not in SPDX:
            raise ModelsError(f"tool {name}: licence {t['licence']!r} is not approved / has no SPDX label")
        if not all(re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", p) for p in t["apt"]):
            raise ModelsError(f"tool {name}: bad apt package name in {t['apt']}")
    for family, spec in models["families"].items():
        for tool in spec.get("tools", []):
            if tool not in tools:
                raise ModelsError(f"{family}: no tool named {tool!r} in models.yaml")
        handler = spec.get("handler")
        if handler is not None and handler not in HANDLERS:
            raise ModelsError(f"{family}: unknown handler {handler!r}; have {sorted(HANDLERS)}")
        for name in spec["files"]:
            if name not in by_name:
                raise ModelsError(f"{family}: no file named {name!r} in models.yaml")
            f = by_name[name]
            if f.get("licence") not in approved:
                raise ModelsError(f"{name}: licence {f.get('licence')!r} is not in approved_licences")
            if not isinstance(approved[f["licence"]], str) or not approved[f["licence"]].strip():
                raise ModelsError(f"approved_licences.{f['licence']}: say who approved it and when")
            if f["licence"] not in SPDX:
                raise ModelsError(f"{name}: no SPDX label for licence {f['licence']!r}")
            parts = f.get("parts", 1)
            if not isinstance(parts, int) or parts < 1:
                raise ModelsError(f"{name}: parts must be a positive integer")
            if parts > 1 and f["size_gb"] / parts > LAYER_CAP_GB:   # unsplit bf16 files compress under the cap
                raise ModelsError(f"{name}: {f['size_gb']} GB in {parts} part(s) exceeds the "
                                  f"{LAYER_CAP_GB} GB per-layer budget; raise `parts`")
            for text in f.get("licence_files", []):
                if not (EXTRAS / "licenses" / text).is_file():
                    raise ModelsError(f"{name}: licence file docker/extras/licenses/{text} is missing")


def family_files(family: str, models: dict | None = None) -> list[dict]:
    models = models or load_models()
    if family not in models["families"]:
        raise KeyError(f"unknown family {family!r}; have {sorted(models['families'])}")
    validate(models)
    by_name = {f["name"]: f for f in models["files"]}
    return [by_name[n] for n in models["families"][family]["files"]]


def family_tools(family: str, models: dict) -> list[dict]:
    """The family's pinned tool archives (models.yaml `tools:`), each with its `name`."""
    tools = models.get("tools") or {}
    return [{"name": n, **tools[n]} for n in models["families"][family].get("tools", [])]


def licence_label(family: str, models: dict) -> str:
    """SPDX expression for the image: the weights' and tools' licences, plus MIT for our handler."""
    ids = {f["licence"] for f in family_files(family, models)}
    ids |= {t["licence"] for t in family_tools(family, models)}
    labels = {SPDX[i] for i in ids}
    if models["families"][family].get("handler"):
        labels.add("MIT")
    return " AND ".join(sorted(labels))


def split_files(family: str, models: dict) -> list[dict]:
    return [f for f in family_files(family, models) if f.get("parts", 1) > 1]


def container_disk_gb(family: str, models: dict | None = None) -> int:
    """Template container disk: the usual scratch space plus room to join each split file
    (its parts stay in the read-only image; the joined copy lives in the writable layer)."""
    models = models or load_models()
    extra = sum(f["size_gb"] for f in split_files(family, models))
    return CONTAINER_DISK_GB + (int(extra) + 1 if extra else 0)


def _stage(f: dict) -> str:
    return "split-" + f["name"].replace("_", "-").replace(".", "-")


def _fetch_run(f: dict, env: str = "", parts: str = "") -> str:
    return (f"# {f['name']}: {f['licence']} ({f['licence_url']})\n"
            "RUN --mount=type=bind,from=fetch,source=fetch.py,target=/tmp/kdp-fetch.py \\\n"
            f"    {env}python3 /tmp/kdp-fetch.py --root {COMFY_ROOT}{parts} \\\n"
            f"      {f['path']} \\\n"
            f"      {f['url']} \\\n"
            f"      {f.get('sha256') or '-'} {f['licence_repo']} {f['licence']}\n")


def split_stages(family: str, models: dict) -> str:
    """Build stages (not shipped) that fetch + verify each split file and cut it into parts."""
    return "".join(
        f"# {f['name']} ({f['size_gb']} GB) is over the layer cap: fetched and verified whole here,\n"
        f"# cut into {f['parts']} parts, each COPYed below as its own layer.\n"
        f"FROM {BASE_IMAGE} AS {_stage(f)}\n"
        + _fetch_run(f, env=f"KDP_MANIFEST=/kdp-split/{f['name']}.json ", parts=f" --parts {f['parts']}")
        + "\n"
        for f in split_files(family, models))


def weight_layers(family: str, models: dict) -> str:
    out = []
    for f in family_files(family, models):
        if f.get("parts", 1) == 1:
            out.append(_fetch_run(f))
            continue
        src = f"{COMFY_ROOT}/{f['path']}.kdp-part"
        out.append(f"# {f['name']}: {f['licence']} ({f['licence_url']}), {f['parts']} layers\n"
                   + "".join(f"COPY --from={_stage(f)} {src}{i} {src}{i}\n" for i in range(f["parts"]))
                   + f"COPY --from={_stage(f)} /kdp-split/{f['name']}.json /kdp-split/{f['name']}.json\n")
    return "\n".join(out)


def tool_layers(family: str, models: dict) -> str:
    """One RUN per tool: its runtime libraries from apt, then the verified archive (tool.py,
    bind-mounted like fetch.py), unpacked and linked into /usr/local/bin, smoke-tested."""
    out = []
    for t in family_tools(family, models):
        out.append(
            f"# {t['name']} {t['version']}: {t['licence']} ({t['licence_url']})\n"
            "RUN --mount=type=bind,from=fetch,source=tool.py,target=/tmp/kdp-tool.py \\\n"
            "    apt-get update \\\n"
            f"    && apt-get install -y --no-install-recommends {' '.join(t['apt'])} \\\n"
            "    && rm -rf /var/lib/apt/lists/* \\\n"
            f"    && python3 /tmp/kdp-tool.py {t['name']} {t['version']} \\\n"
            f"      {t['url']} \\\n"
            f"      {t['sha256']} {t['size']} {t['dest']} {t['binary']} \\\n"
            f"    && {t['binary']} -b --factory-startup --version\n")
    return ("\n" + "\n".join(out)) if out else ""


def extras_layers(family: str, models: dict) -> str:
    """COPY lines for licence texts, the replacement handler and the split-file joiner (empty for
    a weights-only family)."""
    texts = sorted({t for f in family_files(family, models) for t in f.get("licence_files", [])})
    lines = [f"COPY --from=extras licenses/{t} /licenses/{t}" for t in texts]
    handler = models["families"][family].get("handler")
    if handler:
        lines.append(f"# Returns every output kind (3d, video, ...), not only images.\n"
                     f"COPY --from=extras {HANDLERS[handler]} /handler.py")
    if split_files(family, models):
        lines.append(f"# Joins the split weights (needs container disk {container_disk_gb(family, models)} GB), "
                     f"then runs the stock /start.sh.\n"
                     f"COPY --from=extras {ASSEMBLE} /kdp-assemble.py\n"
                     f'CMD ["python3", "/kdp-assemble.py", "/start.sh"]')
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


def tools_note(family: str, models: dict) -> str:
    return "".join(f" + {t['name']} {t['version']}" for t in family_tools(family, models))


def dockerfile(family: str, models: dict | None = None) -> str:
    models = models or load_models()
    runs = weight_layers(family, models)
    has_extras = bool(extras_layers(family, models))
    build_extra = " --build-context extras=docker/extras" if has_extras else ""
    return f"""# GENERATED by images.py from models.yaml; do not edit by hand.
# Build (from the repo root):
#   docker build --build-context fetch=docker/fetch{build_extra} \\
#     -t {image_name(family)}:dev docker/{family}
{split_stages(family, models)}FROM {BASE_IMAGE}

LABEL org.opencontainers.image.source="{SOURCE}" \\
      org.opencontainers.image.description="runpod/worker-comfyui 5.10.0-base + {family} weights{tools_note(family, models)} ({licence_label(family, models)})" \\
      org.opencontainers.image.licenses="{licence_label(family, models)}"

# One RUN (= one layer) per weight file: GHCR caps a layer at 10 GB.
{runs}{tool_layers(family, models)}{extras_layers(family, models)}"""


def render(models: dict | None = None) -> list[Path]:
    models = models or load_models()
    written = []
    for family in models["families"]:
        d = DOCKER / family
        d.mkdir(parents=True, exist_ok=True)
        (d / "Dockerfile").write_text(dockerfile(family, models))
        (d / ".dockerignore").write_text(DOCKERIGNORE)
        written.append(d / "Dockerfile")
    return written


def stale(models: dict | None = None) -> list[str]:
    models = models or load_models()
    out = []
    for family in models["families"]:
        d = DOCKER / family
        for name, want in (("Dockerfile", dockerfile(family, models)), (".dockerignore", DOCKERIGNORE)):
            p = d / name
            if not p.exists() or p.read_text() != want:
                out.append(str(p.relative_to(HERE)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["render", "check", "families", "extras-families"])
    args = ap.parse_args(argv)
    if args.cmd == "render":
        for p in render():
            print(f"wrote {p.relative_to(HERE)}")
    elif args.cmd == "check":
        bad = stale()
        if bad:
            print("stale (run `python images.py render`): " + ", ".join(bad), file=sys.stderr)
            return 1
    elif args.cmd == "families":
        print(" ".join(load_models()["families"]))
    else:
        models = load_models()
        print(" ".join(f for f in models["families"] if extras_layers(f, models)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
