# Wan2.2 character LoRA on a RunPod pod

Trains a LoRA for **Wan2.2 T2V-A14B** (both experts: high-noise and low-noise) from a folder of
captioned images, on one 24 GB GPU pod, and renders three 41-frame test clips at the end.
Nothing here is baked into an image: the pod runs the upstream trainer image and fetches this
folder from GitHub at a pinned commit (`--kit-ref`), so a training run needs no image build.

| Piece | What | Licence |
|---|---|---|
| Trainer | [ostris/ai-toolkit](https://github.com/ostris/ai-toolkit) 0.13.43 (commit 73c84b0), image `ostris/aitoolkit:0.13.43@sha256:ba3327ce…` | MIT |
| Base | `ai-toolkit/Wan2.2-T2V-A14B-Diffusers-bf16` (both transformers), `ai-toolkit/umt5_xxl_encoder`, `ai-toolkit/wan2.1-vae` | Apache-2.0 (cards) |
| 4-bit recovery adapter | `ostris/accuracy_recovery_adapters` / `wan22_14b_t2i_torchao_uint4.safetensors` | inherits the parent model's licence (its README): Apache-2.0 |

Every Hugging Face repo is pinned to a commit in [`hf_pins.tsv`](hf_pins.tsv); the pod refuses
to start if `main` has moved.

**Why not musubi-tuner:** its Wan trainer imports code under `hunyuan_model/` that is licensed
under the Tencent Hunyuan Community License, which does not apply in the EU, UK and South Korea
(checked 2026-11-25: `wan_train_network.py` → `hv_train_network.py` → `musubi_tuner.hunyuan_model`).
ai-toolkit contains no Hunyuan code.

## Files

| File | Runs where | Does |
|---|---|---|
| `run_pod.py` | operator | uploads the dataset zip to R2, creates the pod (RTX 4090 → 3090 → A5000, 24 GB only), polls, enforces `--max-usd`, downloads results, terminates the pod by ID |
| `pod_train.sh` | pod | downloads pinned weights + dataset, fills `train_config.yaml`, runs ai-toolkit, uploads to R2 `runs/<run_id>/` |
| `train_config.yaml` | pod | ai-toolkit job: rank 32, 2000 steps, lr 1e-4, uint4 + ARA, both experts switching every 10 steps, buckets 512/768, samples at the end |
| `r2.py` | both | R2 get/put and the `status.json` the runner polls |

## Run

Needs `RUNPOD_API_KEY` and the four `BUCKET_*` R2 values in the operator's environment, and the
RunPod secrets `r2_endpoint_url`, `r2_bucket`, `r2_access_key_id`, `r2_secret_access_key`
(the pod only sees secret references).

```bash
python training/wan22-lora/run_pod.py --dataset zaffy_v1.zip --kit-ref <commit> \
    --out out/zaffy_v1 --max-usd 4
python training/wan22-lora/run_pod.py --teardown out/zaffy_v1/created.json   # if interrupted
```

Dataset zip: drawn art as `name.png` + `name.txt` (caption) pairs at the top level. Rendered frames
(Blender, img2img) go in an optional `renders/` subfolder; the pod trains it as a second dataset, so
each art frame is seen `--art-repeats` times (default 2) per epoch and each render `--render-repeats`
times (default 1). Caption renders with a style word such as `3d render` so the render look binds to
that word and not to the trigger.

Result: `out/<run>/result/<run_name>/` with `*.safetensors` LoRAs for both experts (ComfyUI
loads them with two `LoraLoaderModelOnly` nodes, one per expert), `samples/`, `pod.log`.

Estimate (not yet measured): weights ~60 GB download + image pull ~15 min, 2000 steps ~2–3 h on
an RTX 4090 with uint4 + low-VRAM offload, samples ~15 min → ~3–3.5 h × $0.34–0.69/h.
