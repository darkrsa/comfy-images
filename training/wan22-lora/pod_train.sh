#!/usr/bin/env bash
# Runs inside a RunPod pod started by run_pod.py from the upstream trainer image
# ostris/aitoolkit:0.13.43 (MIT; ai-toolkit commit 73c84b0 at /app/ai-toolkit).
# Trains a Wan2.2 T2V-A14B character LoRA (high- and low-noise experts) on an image dataset
# from R2, renders the end-of-training samples, and uploads everything to R2 runs/$RUN_ID/.
# Stage changes go to runs/$RUN_ID/status.json, which run_pod.py polls. No RunPod key in here:
# the runner terminates the pod by ID.
#
# Env (from run_pod.py): RUN_ID, KIT_REF (comfy-images commit with this kit), STEPS, RANK, LR,
# BUCKET_ENDPOINT_URL / BUCKET_NAME / BUCKET_ACCESS_KEY_ID / BUCKET_SECRET_ACCESS_KEY (RunPod secrets r2_*).
set -euo pipefail
: "${RUN_ID:?}" "${KIT_REF:?}" "${BUCKET_ENDPOINT_URL:?}" "${BUCKET_NAME:?}"
export STEPS=${STEPS:-2000} RANK=${RANK:-32} LR=${LR:-1e-4} RUN_NAME="zaffy_wan22_t2v_${RUN_ID}"
D=/workspace/data; O=/workspace/out; K=/workspace/kit
mkdir -p $D/images $O $K
exec > >(tee -a $O/pod.log) 2>&1
export HF_HUB_ENABLE_HF_TRANSFER=1 HF_HOME=/workspace/hf

for f in r2.py train_config.yaml hf_pins.tsv; do
  curl -fsSL "https://raw.githubusercontent.com/darkrsa/comfy-images/$KIT_REF/training/wan22-lora/$f" -o $K/$f
done
pip install -q --break-system-packages boto3 hf_transfer
status() { python $K/r2.py status "$RUN_ID" "$1" "${2:-}"; }
trap 'status failed "line $LINENO"; python $K/r2.py put-dir "$RUN_ID" $O || true' ERR
status setup

# 1. weights: download main of each repo and refuse it unless main is still the pinned commit
status download
grep -v '^#' $K/hf_pins.tsv | while IFS=$'\t' read -r repo sha pattern; do
  got=$(python -c "from huggingface_hub import HfApi; print(HfApi().model_info('$repo').sha)")
  [ "$got" = "$sha" ] || { echo "$repo moved: main=$got pinned=$sha"; exit 1; }
  hf download "$repo" --revision "$sha" ${pattern:+--include "$pattern"} >/dev/null
done
python $K/r2.py get "datasets/$RUN_ID.zip" $D/ds.zip
(cd $D/images && unzip -q ../ds.zip) && ls $D/images | head -3 && echo "$(ls $D/images/*.txt | wc -l) captions"

# 2. train (both experts, switching every 10 steps) + end-of-training samples
status train
python -c "import os,sys; sys.stdout.write(os.path.expandvars(open('$K/train_config.yaml').read()))" > $K/job.yaml
cd /app/ai-toolkit && python run.py $K/job.yaml
python $K/r2.py put-dir "$RUN_ID" $O
status done
