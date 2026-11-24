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

## How these are published

`.github/workflows/copy-images` copies a built tag from the build registry into this namespace
registry to registry (no rebuild; layers stay byte-identical, which the workflow verifies) and only
rewrites `org.opencontainers.image.source` to point at this repository.

## Licenses

The files in this repository (README, workflows) are MIT, see [LICENSE](LICENSE).
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
