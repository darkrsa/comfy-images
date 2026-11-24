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
"""
from __future__ import annotations

import argparse
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
DOCKERIGNORE = "# Build context = this directory; only the Dockerfile is sent.\n*\n!Dockerfile\n"
HANDLERS = {"outputs": "handler.py"}   # family `handler:` value -> file in docker/extras
# models.yaml licence id -> SPDX expression for the image label.
SPDX = {"apache-2.0": "Apache-2.0", "mit": "MIT", "dinov3-license": "LicenseRef-DINOv3"}


class ModelsError(ValueError):
    """models.yaml is inconsistent: unknown or unapproved licence, missing file, bad handler."""


def image_name(family: str) -> str:
    return f"{REGISTRY}/kdp-comfy-{family}"


def validate(models: dict) -> None:
    """Raise ModelsError unless every family's files exist, carry an approved licence with an
    SPDX label, ship their licence texts, and name a known handler."""
    approved = models.get("approved_licences") or {}
    by_name = {f["name"]: f for f in models["files"]}
    for family, spec in models["families"].items():
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


def licence_label(family: str, models: dict) -> str:
    """SPDX expression for the image: the weights' licences, plus MIT for our own handler."""
    ids = {f["licence"] for f in family_files(family, models)}
    labels = {SPDX[i] for i in ids}
    if models["families"][family].get("handler"):
        labels.add("MIT")
    return " AND ".join(sorted(labels))


def extras_layers(family: str, models: dict) -> str:
    """COPY lines for licence texts and the replacement handler (empty for a weights-only family)."""
    texts = sorted({t for f in family_files(family, models) for t in f.get("licence_files", [])})
    lines = [f"COPY --from=extras licenses/{t} /licenses/{t}" for t in texts]
    handler = models["families"][family].get("handler")
    if handler:
        lines.append(f"# Returns every output kind (3d, video, ...), not only images.\n"
                     f"COPY --from=extras {HANDLERS[handler]} /handler.py")
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


def dockerfile(family: str, models: dict | None = None) -> str:
    files = family_files(family, models)
    runs = "\n".join(
        f"# {f['name']}: {f['licence']} ({f['licence_url']})\n"
        "RUN --mount=type=bind,from=fetch,source=fetch.py,target=/tmp/kdp-fetch.py \\\n"
        f"    python3 /tmp/kdp-fetch.py --root {COMFY_ROOT} \\\n"
        f"      {f['path']} \\\n"
        f"      {f['url']} \\\n"
        f"      {f.get('sha256') or '-'} {f['licence_repo']} {f['licence']}\n"
        for f in files)
    models = models or load_models()
    has_extras = bool(extras_layers(family, models))
    build_extra = " --build-context extras=docker/extras" if has_extras else ""
    return f"""# GENERATED by images.py from models.yaml; do not edit by hand.
# Build (from the repo root):
#   docker build --build-context fetch=docker/fetch{build_extra} \\
#     -t {image_name(family)}:dev docker/{family}
FROM {BASE_IMAGE}

LABEL org.opencontainers.image.source="{SOURCE}" \\
      org.opencontainers.image.description="runpod/worker-comfyui 5.10.0-base + {family} weights ({licence_label(family, models)})" \\
      org.opencontainers.image.licenses="{licence_label(family, models)}"

# One RUN (= one layer) per weight file: GHCR caps a layer at 10 GB.
{runs}{extras_layers(family, models)}"""


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
