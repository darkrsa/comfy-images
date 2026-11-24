"""API-format workflows in workflows/: links resolve, and every model file they load is baked into
the family the workflow is for (a missing file is a validation error on the endpoint, after a
cold start that costs money)."""
import json
from pathlib import Path

import pytest

from images import HERE, load_models

MODELS = load_models()
# workflow file -> family whose image it runs on
WORKFLOWS = {"trellis2_image_to_glb.json": "trellis2"}
LOADER_INPUTS = {"unet_name", "vae_name", "clip_name", "bg_removal_name", "model_name", "ckpt_name"}


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
            if key in LOADER_INPUTS:
                loaded.add(value)
    assert loaded and loaded <= baked, loaded - baked
    savers = [n for n in workflow.values() if n["class_type"] == "SaveGLB"]
    assert savers, "API workflows save meshes with SaveGLB (Save3DAdvanced needs the web UI)"


def test_every_workflow_is_listed():
    assert sorted(p.name for p in (HERE / "workflows").glob("*.json")) == sorted(WORKFLOWS)
