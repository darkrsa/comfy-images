# comfy-images

Ready-to-run [RunPod serverless](https://docs.runpod.io/serverless/overview) images for ComfyUI
text-to-image, ControlNet, image-to-video and image-to-3D: the stock [`runpod/worker-comfyui`](https://github.com/runpod-workers/worker-comfyui)
`5.10.0-base` image with one model family's weights baked in, so an endpoint needs no network volume.

| Image | Weights |
|---|---|
| `ghcr.io/darkrsa/kdp-comfy-flux2klein` | FLUX.2 klein 4B + its Qwen3-4B text encoder + FLUX.2 VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-turbo` | Z-Image Turbo bf16 + Qwen3-4B text encoder + VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-full` | Z-Image bf16 + Qwen3-4B text encoder + VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-turbo-cn` | Z-Image Turbo + Z-Image-Turbo Fun ControlNet Union (ModelPatchLoader) |
| `ghcr.io/darkrsa/kdp-comfy-wan22-5b` | Wan2.2 TI2V-5B fp16 + umT5-XXL fp8 + Wan2.2 VAE; returns video files (all-outputs handler) |
| `ghcr.io/darkrsa/kdp-comfy-trellis2` | TRELLIS.2 (int8) + shape/texture VAEs + DINOv3 ViT-L encoder + BiRefNet; returns GLB (see below) |

Tags: `<yyyymmdd>-<sha>` (immutable) and `latest`. Weight files are placed where ComfyUI expects them:

```
/comfyui/models/unet/  z_image_bf16 | z_image_turbo_bf16 | flux-2-klein-4b  .safetensors
/comfyui/models/clip/  qwen_3_4b.safetensors
/comfyui/models/vae/   ae.safetensors | flux2-vae.safetensors
                       trellis_2_shape_vae_bf16 | trellis_2_texture_vae_bf16  .safetensors
/comfyui/models/diffusion_models/  trellis_2_int8_convrot | wan2.2_ti2v_5B_fp16  .safetensors
/comfyui/models/text_encoders/     umt5_xxl_fp8_e4m3fn_scaled.safetensors
/comfyui/models/vae/               wan2.2_vae.safetensors
/comfyui/models/model_patches/     Z-Image-Turbo-Fun-Controlnet-Union.safetensors
/comfyui/models/clip_vision/       dino_v3_vit_l.safetensors
/comfyui/models/background_removal/ birefnet.safetensors
/licenses/             licence texts a bundled weight requires (trellis2: DINOv3)
```

Each image is the upstream worker image plus weight files, each downloaded from Hugging Face at
build time, checked against a pinned sha256, and refused unless its model card reports the licence
`models.yaml` declares for it, which must be in `approved_licences` there (anything other than
Apache-2.0 is approved by the owner explicitly). The only code an image adds is the output handler
of families that set `handler: outputs` (below). Each image was scanned (gitleaks) before publishing.

## trellis2 and wan22-5b: all outputs (GLB, video)

The stock worker-comfyui 5.10.0 handler returns only the `images` output of each node, so a
`SaveGLB` node (which reports its file under `3d`) gives an empty result. The `trellis2` and
`wan22-5b` images replace `/handler.py` with [`docker/extras/handler.py`](docker/extras/handler.py), which returns
every file saved to ComfyUI's output folder, whatever its kind:

```json
{"input": {"workflow": {"...": "ComfyUI API-format prompt with LoadImage + ... + SaveGLB"},
           "images": [{"name": "zaffy.png", "image": "<base64>"}]}}
→ {"output": {"files": [{"node": "9", "kind": "3d", "filename": "mesh_00001_.glb", "bytes": 1234,
                          "key": "results/<job>/3d/mesh_00001_.glb", "url": "<presigned GET>",
                          "stored": null, "base64": "<... only while the reply fits>"}],
               "warnings": []}}
```

**A finished output is never dropped for its size.** Every file is first uploaded to an
S3-compatible bucket (Cloudflare R2) when `BUCKET_ENDPOINT_URL`, `BUCKET_NAME`,
`BUCKET_ACCESS_KEY_ID` and `BUCKET_SECRET_ACCESS_KEY` are set (as RunPod secret references,
`{{ RUNPOD_SECRET_r2_* }}`; `runpod_smoke.py --r2` sets them), and/or written under `RESULT_DIR`
(a mounted network volume). Files are inlined as base64 while the reply stays under
`MAX_RESULT_MB` (default 9; RunPod allows 10 MB for `/run`, 20 MB for `/runsync`); a larger file
comes back by `url` (valid `RESULT_URL_TTL_S`, 7 days) with a warning. With no bucket and no
`RESULT_DIR` such a file is reported in `warnings` as lost. Some but not all `BUCKET_*` set is a
configuration error. Previews (`type: temp`) are not returned; a file reported twice is returned once.
A workflow with any invalid node fails the job (ComfyUI would otherwise run the valid branches
and drop the rest). On a timeout or failure the prompt is removed from ComfyUI's queue and
interrupted, so a warm worker's next job does not wait behind it. Settings must be positive
numbers or the worker refuses to start. Use `SaveGLB` in API workflows; `Save3DAdvanced`
needs the web UI's viewport state. Other settings: `COMFY_STARTUP_TIMEOUT_S` (600),
`COMFY_JOB_TIMEOUT_S` (1800). The ComfyUI template (upsample 1536, remesh 768, 700k faces) does not fit 24 GB: OOM in VaeDecodeShapeTrellis at 1536 and in RemeshMesh at 768 (2026-11-24, AMPERE_24); the workflow uses 1024 / 512 / 80k.

## How these are built

| Path | Purpose |
|---|---|
| `models.yaml` | Every weight file (URL, sha256, size, licence card) and which family bakes which files |
| `images.py` | Renders `docker/<family>/Dockerfile` + `.dockerignore` from models.yaml (`render`, `check`, `families`) |
| `docker/fetch/fetch.py` | Build-time downloader (stdlib only), bind-mounted into each `RUN`, never copied into a layer. Refuses a file whose upstream card does not report its declared licence; verifies size + sha256 |
| `docker/extras/` | Copied into images that need it: `licenses/` (texts a weight licence requires) and `handler.py` (all-outputs handler) |
| `ci/` | Runner disk setup, which families a push rebuilds (`plan_families.sh`), and the per-image review manifest (files added vs the base, for the secret scan) |
| `workflows/` | API-format ComfyUI workflows for the images (`trellis2_image_to_glb.json`: photo → background removal → TRELLIS.2 1024 → remesh/decimate 80k → baked 1024 PBR textures → `SaveGLB`) |
| `runpod_smoke.py` | One test run on RunPod: template + endpoint (min 0, max 1 worker), one job under a dollar cap, outputs saved, both deleted by ID, estimate printed |
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
docker build --build-context fetch=docker/fetch --build-context extras=docker/extras \
  -t kdp-comfy-trellis2:dev docker/trellis2
```

The first published tag, `20261123-2fbb16c`, was built in a private registry and copied here
registry to registry (layers byte-identical, only the `source` label rewritten).

## Licenses

The files in this repository (scripts, Dockerfiles, workflows, docs, the output handler) are MIT,
see [LICENSE](LICENSE).
The images bundle third-party software and weights under their own licenses:

| Component | License | Source |
|---|---|---|
| runpod/worker-comfyui 5.10.0 (handler) | AGPL-3.0 | https://github.com/runpod-workers/worker-comfyui/tree/5.10.0 |
| ComfyUI (inside the base image) | GPL-3.0 | https://github.com/comfyanonymous/ComfyUI |
| Z-Image (bf16) | Apache-2.0 | https://huggingface.co/Tongyi-MAI/Z-Image |
| Z-Image Turbo (bf16) and its VAE | Apache-2.0 | https://huggingface.co/Tongyi-MAI/Z-Image-Turbo |
| Qwen3-4B (text encoder) | Apache-2.0 | https://huggingface.co/Qwen/Qwen3-4B |
| FLUX.2 [klein] 4B, its text encoder and the FLUX.2 VAE | Apache-2.0 | https://huggingface.co/black-forest-labs/FLUX.2-klein-4B |
| TRELLIS.2 4B (int8) and its shape/texture VAEs | MIT | https://huggingface.co/microsoft/TRELLIS.2-4B |
| DINOv3 ViT-L (image encoder for TRELLIS.2) | DINOv3 License (Meta), text in the image at `/licenses/DINOv3-LICENSE.md`; no ITAR/military use | https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md |
| BiRefNet (background removal) | MIT | https://huggingface.co/ZhengPeng7/BiRefNet |
| Z-Image-Turbo Fun ControlNet Union | Apache-2.0 | https://huggingface.co/alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union |
| Wan2.2 TI2V-5B and the Wan2.2 VAE | Apache-2.0 | https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B |
| umT5-XXL (text encoder for Wan) | Apache-2.0 | https://huggingface.co/google/umt5-xxl |

Weight files are redistributed unmodified (ComfyUI-packaged single files from
[Comfy-Org](https://huggingface.co/Comfy-Org)). Corresponding source for the AGPL/GPL components
is available at the links above; these images add no modifications to them.
