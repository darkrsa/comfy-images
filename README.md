# comfy-images

Ready-to-run [RunPod serverless](https://docs.runpod.io/serverless/overview) images for ComfyUI
text-to-image, ControlNet, image-to-video, lipsync (talking video from a portrait + speech) and image-to-3D: the stock [`runpod/worker-comfyui`](https://github.com/runpod-workers/worker-comfyui)
`5.10.0-base` image with one model family's weights baked in, so an endpoint needs no network volume.

| Image | Weights |
|---|---|
| `ghcr.io/darkrsa/kdp-comfy-flux2klein` | FLUX.2 klein 4B + its Qwen3-4B text encoder + FLUX.2 VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-turbo` | Z-Image Turbo bf16 + Qwen3-4B text encoder + VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-full` | Z-Image bf16 + Qwen3-4B text encoder + VAE |
| `ghcr.io/darkrsa/kdp-comfy-zimage-turbo-cn` | Z-Image Turbo + Z-Image-Turbo Fun ControlNet Union (ModelPatchLoader) |
| `ghcr.io/darkrsa/kdp-comfy-wan22-5b` | Wan2.2 TI2V-5B fp16 + umT5-XXL fp8 + Wan2.2 VAE; returns video files (all-outputs handler) |
| `ghcr.io/darkrsa/kdp-comfy-trellis2` | TRELLIS.2 (int8) + shape/texture VAEs + DINOv3 ViT-L encoder + BiRefNet; returns GLB (see below) |
| `ghcr.io/darkrsa/kdp-comfy-trellis2-pod` | Same weights and handler as `trellis2`, plus headless Blender 4.5.17 LTS (`blender -b`): a RunPod **pod** image (see below) |
| `ghcr.io/darkrsa/kdp-comfy-wan22-s2v` | Wan2.2-S2V 14B fp8 (2 layers, see below) + wav2vec2-large English + umT5-XXL fp8 + Wan2.1 VAE + LightX2V 4-step LoRA; returns mp4 with the speech |
| `ghcr.io/darkrsa/kdp-comfy-wan21-infinitetalk` | Wan2.1 I2V 480p 14B fp8 (2 layers) + InfiniteTalk patch + Chinese wav2vec2 base + umT5-XXL fp8 + Wan2.1 VAE + LightX2V distill LoRA; returns mp4 |

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
/comfyui/models/diffusion_models/  wan2.2_s2v_14B_fp8_scaled | wan2.1_i2v_480p_14B_fp8_scaled  .safetensors
/comfyui/models/model_patches/     wan2.1_infiniteTalk_multi_fp16.safetensors
/comfyui/models/audio_encoders/    wav2vec2_large_english_fp16 | wav2vec2-chinese-base_fp16  .safetensors
/comfyui/models/loras/             wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise |
                                   lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16  .safetensors
/comfyui/models/vae/               wan_2.1_vae.safetensors
/comfyui/models/clip_vision/       dino_v3_vit_l.safetensors
/comfyui/models/background_removal/ birefnet.safetensors
/licenses/             licence texts a bundled weight requires (trellis2: DINOv3)
/opt/blender/          trellis2-pod: Blender 4.5.17 LTS, `blender` on PATH; /kdp-tools.json records it
```

Each image is the upstream worker image plus weight files, each downloaded from Hugging Face at
build time, checked against a pinned sha256, and refused unless its model card reports the licence
`models.yaml` declares for it, which must be in `approved_licences` there (anything other than
Apache-2.0 is approved by the owner explicitly). The only code an image adds is the output handler
of families that set `handler: outputs` (below). Each image was scanned (gitleaks) before publishing.

## trellis2-pod: a work pod with ComfyUI and Blender

`trellis2-pod` is for an on-demand **pod**, not a serverless endpoint: TRELLIS.2 runs in ComfyUI
and the GLB goes straight into headless Blender (retopology, projection, renders) on the same GPU,
with nothing to install by hand, so the pod can be deleted after a session and recreated later.

- Blender is the unmodified official archive (`models.yaml` `tools:`), downloaded at build time by
  [`docker/fetch/tool.py`](docker/fetch/tool.py) and refused unless its byte size and sha256 match
  the pinned values (from blender.org's `.sha256` file). The build ends with `blender -b --version`.
- Start the pod with env `SERVE_API_LOCALLY=true` and port `8188/http`: ComfyUI then listens on
  0.0.0.0 and is reachable at `https://<pod-id>-8188.proxy.runpod.net` (UI and API). Container
  disk 40 GB.
- Keep work files (GLB, `.blend`, renders) in R2, not on a network volume: a volume ties the pod
  to one datacenter, and GPU stock moves between them.

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

## wan22-s2v and wan21-infinitetalk: lipsync, and weights over the layer cap

API workflows: [`workflows/wan22_s2v_talking.json`](workflows/wan22_s2v_talking.json) (77 frames,
16 fps, ~4.8 s) and [`workflows/wan21_infinitetalk_talking.json`](workflows/wan21_infinitetalk_talking.json)
(81 frames, 25 fps, ~3.2 s): portrait `zaffy.png` + speech `voice.wav` (both sent in `input.images`,
which uploads any file to ComfyUI's input folder) → 480×832 video with the speech muxed in, saved by
`SaveVideo` and returned by the all-outputs handler. One chunk each, distilled (4 / 6 steps, cfg 1),
sized for 24 GB; longer speech needs extend chunks (`WanSoundImageToVideoExtend`, or
`WanInfiniteTalkToVideo` with `previous_frames`), as in the ComfyUI templates
`video_wan2_2_14B_s2v` / `video_wan2_1_infinitetalk`. Not yet run on RunPod.

Each 14B fp8 model is ~16.4 GB, over GHCR's ~10 GB layer cap. models.yaml sets `parts: 2`: a build
stage downloads the whole file and verifies its pinned sha256 (as for every file), cuts it into
`<file>.kdp-part0/1`, and the image COPYs each part as its own layer (the build stage is not
shipped). The image's CMD is [`docker/extras/assemble.py`](docker/extras/assemble.py): before the
stock `/start.sh` it joins the parts back into `<file>` (size-checked; `KDP_VERIFY_SHA256=1` also
re-hashes) in the container's writable layer, so these templates need **37 GB** container disk
instead of 20 (`images.container_disk_gb`; `runpod_smoke.py` sets it). The join copies 16.4 GB on
every cold start: seconds on NVMe, more on a slow disk (not yet measured on RunPod).

## How these are built

| Path | Purpose |
|---|---|
| `models.yaml` | Every weight file (URL, sha256, size, licence card) and which family bakes which files |
| `images.py` | Renders `docker/<family>/Dockerfile` + `.dockerignore` from models.yaml (`render`, `check`, `families`) |
| `docker/fetch/fetch.py` | Build-time downloader (stdlib only), bind-mounted into each `RUN`, never copied into a layer. Refuses a file whose upstream card does not report its declared licence; verifies size + sha256 |
| `docker/extras/` | Copied into images that need it: `licenses/` (texts a weight licence requires), `handler.py` (all-outputs handler) and `assemble.py` (joins split weights at start) |
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
| Wan2.2-S2V 14B | Apache-2.0 | https://huggingface.co/Wan-AI/Wan2.2-S2V-14B |
| Wan2.1 I2V 14B 480p and the Wan2.1 VAE | Apache-2.0 | https://huggingface.co/Wan-AI/Wan2.1-I2V-14B-480P |
| InfiniteTalk (model patch) | Apache-2.0 | https://huggingface.co/MeiGen-AI/InfiniteTalk |
| wav2vec2-large-xlsr-53-english (audio encoder for S2V) | Apache-2.0 | https://huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-english |
| chinese-wav2vec2-base (audio encoder for InfiniteTalk) | MIT | https://huggingface.co/TencentGameMate/chinese-wav2vec2-base |
| Blender 4.5.17 LTS (trellis2-pod only; official linux-x64 archive, its licence texts ship inside `/opt/blender`) | GPL-2.0-or-later | https://www.blender.org/about/license/, source: https://download.blender.org/source/ |
| LightX2V LoRAs (Wan2.2 Lightning 4-step; Wan2.1 I2V 480p step/CFG distill) | Apache-2.0 | https://huggingface.co/lightx2v/Wan2.2-Lightning, https://huggingface.co/lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v |

Weight files are redistributed unmodified (ComfyUI-packaged single files from
[Comfy-Org](https://huggingface.co/Comfy-Org), and two from [Kijai](https://huggingface.co/Kijai):
the Chinese wav2vec2 and the Wan2.1 LightX2V LoRA). A 14B model is shipped as two byte ranges that
the image joins back into the identical file (same sha256) before use. Corresponding source for the AGPL/GPL components
is available at the links above; these images add no modifications to them.
