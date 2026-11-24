# comfy-images

Ready-to-run [RunPod serverless](https://docs.runpod.io/serverless/overview) images for ComfyUI
text-to-image: the stock [`runpod/worker-comfyui`](https://github.com/runpod-workers/worker-comfyui)
`5.10.0-base` image with one model family's weights baked in, so an endpoint needs no network volume.

| Image | Weights |
|---|---|
| `ghcr.io/darkrsa/kdp-comfy-flux2klein` | FLUX.2 klein 4B + its Qwen3-4B text encoder + FLUX.2 VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-turbo` | Z-Image Turbo bf16 + Qwen3-4B text encoder + VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-full` | Z-Image bf16 + Qwen3-4B text encoder + VAE |

Tags: `<yyyymmdd>-<sha>` (immutable) and `latest`. Weight files are placed where ComfyUI expects them:

```
/comfyui/models/unet/  z_image_bf16 | z_image_turbo_bf16 | flux-2-klein-4b  .safetensors
/comfyui/models/clip/  qwen_3_4b.safetensors
/comfyui/models/vae/   ae.safetensors | flux2-vae.safetensors
```

The images contain no application code: only the upstream worker image plus weight files, each
downloaded from Hugging Face at build time, checked against a pinned sha256, and refused unless
its model card reports `license: apache-2.0`. Each image was scanned (gitleaks) before publishing.

## How these are built

| Path | Purpose |
|---|---|
| `models.yaml` | Every weight file (URL, sha256, size, licence card) and which family bakes which files |
| `images.py` | Renders `docker/<family>/Dockerfile` + `.dockerignore` from models.yaml (`render`, `check`, `families`) |
| `docker/fetch/fetch.py` | Build-time downloader (stdlib only), bind-mounted into each `RUN`, never copied into a layer. Refuses non-Apache upstream cards; verifies size + sha256 |
| `ci/` | Runner disk setup and the per-image review manifest (files added vs the base, for the secret scan) |
| `.github/workflows/build.yml` | Builds, secret-scans (gitleaks, fails on any finding) and pushes one image per family |
| `.github/workflows/test.yml` | Offline tests + "Dockerfiles match models.yaml" on every push and PR |

Build: Actions → **build-images** → Run workflow (a family or `all`), or merge a change under
`docker/**` / `ci/**` to `main` (only the touched families rebuild). To change weights, edit
`models.yaml`, run `python images.py render` and commit both.

Each push gets `<yyyymmdd>-<shortsha>` and `latest`. Point RunPod templates at a dated tag
(kdp-studio: `python setup.py template --tag <tag>` in `infra/runpod-serverless/`), not `latest`.

Local build (~70 GB free):

```bash
docker build --build-context fetch=docker/fetch -t kdp-comfy-zimage-turbo:dev docker/zimage-turbo
```

The first published tag, `20261123-2fbb16c`, was built in a private registry and copied here
registry to registry (layers byte-identical, only the `source` label rewritten).

## Licenses

The files in this repository (scripts, Dockerfiles, workflows, docs) are MIT, see [LICENSE](LICENSE).
The images bundle third-party software and weights under their own licenses:

| Component | License | Source |
|---|---|---|
| runpod/worker-comfyui 5.10.0 (handler) | AGPL-3.0 | https://github.com/runpod-workers/worker-comfyui/tree/5.10.0 |
| ComfyUI (inside the base image) | GPL-3.0 | https://github.com/comfyanonymous/ComfyUI |
| Z-Image (bf16) | Apache-2.0 | https://huggingface.co/Tongyi-MAI/Z-Image |
| Z-Image Turbo (bf16) and its VAE | Apache-2.0 | https://huggingface.co/Tongyi-MAI/Z-Image-Turbo |
| Qwen3-4B (text encoder) | Apache-2.0 | https://huggingface.co/Qwen/Qwen3-4B |
| FLUX.2 [klein] 4B, its text encoder and the FLUX.2 VAE | Apache-2.0 | https://huggingface.co/black-forest-labs/FLUX.2-klein-4B |

Weight files are redistributed unmodified (ComfyUI-packaged single files from
[Comfy-Org](https://huggingface.co/Comfy-Org)). Corresponding source for the AGPL/GPL components
is available at the links above; these images add no modifications to them.
