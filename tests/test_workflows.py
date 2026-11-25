"""API-format workflows in workflows/: links resolve, and every model file they load is baked into
the family the workflow is for (a missing file is a validation error on the endpoint, after a
cold start that costs money)."""
import json
from pathlib import Path

import pytest

from images import HERE, load_models

MODELS = load_models()
# workflow file -> family whose image it runs on
WORKFLOWS = {"trellis2_image_to_glb.json": "trellis2",
             "wan22_s2v_talking.json": "wan22-s2v",
             "wan21_infinitetalk_talking.json": "wan21-infinitetalk"}
LOADER_INPUTS = {"unet_name", "vae_name", "clip_name", "bg_removal_name", "model_name", "ckpt_name",
                 "lora_name", "audio_encoder_name"}
# The saver each family's API workflow must end in (the all-outputs handler returns its file).
SAVERS = {"trellis2": "SaveGLB", "wan22-s2v": "SaveVideo", "wan21-infinitetalk": "SaveVideo"}


@pytest.mark.parametrize(("name", "family"), WORKFLOWS.items())
def test_links_resolve_and_models_are_baked(name, family):
    workflow = json.loads((HERE / "workflows" / name).read_text(encoding="utf-8"))
    by_name = {f["name"]: f for f in MODELS["files"]}
    baked = {Path(by_name[n]["path"]).name for n in MODELS["families"][family]["files"]}
    loaded = set()
    for node_id, node in workflow.items():
        assert set(node) == {"class_type", "inputs"}, node_id
        for key, value in node["inputs"].items():
            if isinstance(value, list):
                assert len(value) == 2 and value[0] in workflow and isinstance(value[1], int), (node_id, key)
            if key in LOADER_INPUTS or (node["class_type"] == "ModelPatchLoader" and key == "name"):
                loaded.add(value)
    assert loaded and loaded <= baked, loaded - baked
    savers = [n for n in workflow.values() if n["class_type"] == SAVERS[family]]
    assert savers, f"{name}: no {SAVERS[family]} (Save3DAdvanced needs the web UI; use SaveGLB)"


def test_every_workflow_is_listed():
    assert sorted(p.name for p in (HERE / "workflows").glob("*.json")) == sorted(WORKFLOWS)


@pytest.mark.parametrize("name", ["wan22_s2v_talking.json", "wan21_infinitetalk_talking.json"])
def test_lipsync_workflows_fit_24gb_defaults(name):
    """480p portrait, one chunk, distilled 4-6 steps at cfg 1 (the 14B fp8 model + patch nearly
    fill 24 GB; ComfyUI offloads the rest). Inputs are the names the smoke run uploads."""
    workflow = json.loads((HERE / "workflows" / name).read_text(encoding="utf-8"))
    by_type = {n["class_type"]: n["inputs"] for n in workflow.values()}
    assert by_type["LoadImage"]["image"] == "zaffy.png"
    assert by_type["LoadAudio"]["audio"] == "voice.wav"
    video = by_type.get("WanSoundImageToVideo") or by_type["WanInfiniteTalkToVideo"]
    assert (video["width"], video["height"]) == (480, 832) and video["length"] <= 81
    sampler = by_type.get("KSampler") or by_type["CFGGuider"]
    assert sampler["cfg"] == 1.0
    steps = (by_type.get("KSampler") or by_type["BasicScheduler"])["steps"]
    assert steps <= 6
    assert isinstance(by_type["CreateVideo"]["audio"], list)   # speech muxed into the mp4
