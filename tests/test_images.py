"""Dockerfile + build workflow lint-level checks. No Docker needed."""
import copy
import re
import shutil
import subprocess

import pytest
import yaml

import images as I
from images import HERE, load_models

MODELS = load_models()
FAMILIES = list(MODELS["families"])
WORKFLOW = HERE / ".github" / "workflows" / "build.yml"


DIFFUSION_DIRS = ("models/unet/", "models/diffusion_models/")


def test_families_have_one_diffusion_model_each():
    assert FAMILIES == ["zimage-full", "zimage-turbo", "flux2klein", "zimage-turbo-cn", "wan22-5b", "trellis2", "trellis2-pod", "wan22-s2v", "wan21-infinitetalk"]
    by_name = {f["name"]: f for f in MODELS["files"]}
    for fam, spec in MODELS["families"].items():
        models = [n for n in spec["files"] if by_name[n]["path"].startswith(DIFFUSION_DIRS)]
        assert len(models) == 1, fam
        assert len({by_name[n]["path"] for n in spec["files"]}) == len(spec["files"])
    for prof in MODELS["profiles"].values():
        assert set(prof["files"]) == set(MODELS["families"][prof["family"]]["files"])


def test_every_licence_in_use_is_approved_with_a_source():
    approved = MODELS["approved_licences"]
    assert all(isinstance(source, str) and source for source in approved.values())
    used = {f["licence"] for f in MODELS["files"]}
    assert used <= set(approved)
    assert {"mit", "dinov3-license"} <= used   # trellis2 (owner approval 2026-11-24)


def test_dinov3_ships_its_licence_text():
    dino = next(f for f in MODELS["files"] if f["name"] == "dino_v3_vit_l")
    assert dino["licence"] == "dinov3-license"
    assert "DINOv3-LICENSE.md" in dino["licence_files"]
    text = (I.EXTRAS / "licenses" / "DINOv3-LICENSE.md").read_text(encoding="utf-8")
    assert text.startswith("# DINOv3 License")
    dockerfile = (I.DOCKER / "trellis2" / "Dockerfile").read_text()
    assert "COPY --from=extras licenses/DINOv3-LICENSE.md /licenses/DINOv3-LICENSE.md" in dockerfile


def _models_with(**changes):
    models = copy.deepcopy(MODELS)
    by_name = {f["name"]: f for f in models["files"]}
    for name, fields in changes.items():
        by_name[name].update(fields)
    return models


def test_unapproved_licence_is_refused():
    models = _models_with(birefnet={"licence": "cc-by-nc-4.0"})
    with pytest.raises(I.ModelsError, match="'cc-by-nc-4.0' is not in approved_licences"):
        I.dockerfile("trellis2", models)


def test_removing_an_approval_refuses_the_family():
    models = copy.deepcopy(MODELS)
    del models["approved_licences"]["dinov3-license"]
    with pytest.raises(I.ModelsError, match="dino_v3_vit_l"):
        I.dockerfile("trellis2", models)


def test_missing_licence_text_is_refused():
    models = _models_with(dino_v3_vit_l={"licence_files": ["NOPE.md"]})
    with pytest.raises(I.ModelsError, match="NOPE.md is missing"):
        I.dockerfile("trellis2", models)


def test_approval_without_who_and_when_is_refused():
    models = copy.deepcopy(MODELS)
    models["approved_licences"]["mit"] = None
    with pytest.raises(I.ModelsError, match="approved_licences.mit: say who"):
        I.dockerfile("trellis2", models)


def test_unknown_handler_is_refused():
    models = copy.deepcopy(MODELS)
    models["families"]["trellis2"]["handler"] = "images"
    with pytest.raises(I.ModelsError, match="unknown handler 'images'"):
        I.dockerfile("trellis2", models)


def test_licence_label_and_extras_per_family():
    assert I.licence_label("zimage-turbo", MODELS) == "Apache-2.0"
    assert I.licence_label("trellis2", MODELS) == "LicenseRef-DINOv3 AND MIT"
    assert I.extras_layers("zimage-turbo", MODELS) == ""
    assert "COPY --from=extras handler.py /handler.py" in I.extras_layers("trellis2", MODELS)


def test_tool_layers_pin_the_archive_and_smoke_test_it():
    text = (I.DOCKER / "trellis2-pod" / "Dockerfile").read_text()
    t = MODELS["tools"]["blender"]
    run = re.search(r"^RUN --mount=type=bind,from=fetch,source=tool\.py,target=/tmp/kdp-tool\.py (.*?)(?=^\s*$|^#|\Z)",
                    text, re.S | re.M).group(1).replace("\\\n", " ").split()
    i = run.index("/tmp/kdp-tool.py")
    assert run[i + 1:i + 8] == ["blender", t["version"], t["url"], t["sha256"], str(t["size"]), t["dest"], t["binary"]]
    assert run[run.index("install"):run.index("install") + 3] == ["install", "-y", "--no-install-recommends"]
    assert run[-4:] == ["blender", "-b", "--factory-startup", "--version"]
    assert t["url"].startswith("https://download.blender.org/release/")
    assert re.fullmatch(r"[0-9a-f]{64}", t["sha256"])
    assert I.licence_label("trellis2-pod", MODELS) == "GPL-2.0-or-later AND LicenseRef-DINOv3 AND MIT"
    assert "blender 4.5.17" in text.split("LABEL", 1)[1].split("\n\n", 1)[0]
    for family, spec in MODELS["families"].items():   # only the pod image carries a tool
        assert ("/tmp/kdp-tool.py" in (I.DOCKER / family / "Dockerfile").read_text()) == bool(spec.get("tools"))
    assert MODELS["families"]["trellis2-pod"]["files"] == MODELS["families"]["trellis2"]["files"]


@pytest.mark.parametrize(("change", "match"), [
    (lambda m: m["families"]["trellis2-pod"]["tools"].append("houdini"), "no tool named 'houdini'"),
    (lambda m: m["tools"]["blender"].update(sha256="-"), "pinned sha256"),
    (lambda m: m["tools"]["blender"].update(url="http://download.blender.org/x.tar.xz"), "https url"),
    (lambda m: m["tools"]["blender"].update(size="big"), "exact byte count"),
    (lambda m: m["tools"]["blender"].update(licence="proprietary"), "not approved"),
    (lambda m: m["approved_licences"].pop("gpl-2.0-or-later"), "not approved"),
    (lambda m: m["tools"]["blender"].update(apt=["libgl1; rm -rf /"]), "bad apt package"),
    (lambda m: m["tools"]["blender"].pop("dest"), "keys"),
])
def test_bad_tool_is_refused(change, match):
    models = copy.deepcopy(MODELS)
    change(models)
    with pytest.raises(I.ModelsError, match=match):
        I.validate(models)


def test_committed_dockerfiles_are_rendered_from_models_yaml():
    assert I.stale() == []


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_file_is_fetched_with_sha256_check(family):
    text = (I.DOCKER / family / "Dockerfile").read_text()
    runs = re.findall(r"^RUN (.*?)(?=^\s*$|^#|\Z)", text, re.S | re.M)
    runs = [r for r in runs if "/tmp/kdp-tool.py" not in r]   # tool layers: test_tool_layers_*
    files = I.family_files(family, MODELS)
    assert len(runs) == len(files)   # one layer per weight file
    for f, run in zip(files, runs):
        args = run.replace("\\\n", " ").split()
        assert "--mount=type=bind,from=fetch,source=fetch.py,target=/tmp/kdp-fetch.py" in args
        i = args.index("/tmp/kdp-fetch.py")
        parts = ["--parts", str(f["parts"])] if f.get("parts", 1) > 1 else []
        assert args[i + 1:] == ["--root", "/comfyui", *parts, f["path"], f["url"],
                                f.get("sha256") or "-", f["licence_repo"], f["licence"]]
        if parts:   # the split stage writes its own manifest, COPYed next to the parts
            assert args[i - 2] == f"KDP_MANIFEST=/kdp-split/{f['name']}.json"
            assert f["sha256"], "a split file must pin its sha256"
        assert f["url"].startswith("https://huggingface.co/")
        assert f["licence"] in MODELS["approved_licences"]


@pytest.mark.parametrize("family", FAMILIES)
def test_image_adds_nothing_but_weights_licences_and_handler(family):
    d = I.DOCKER / family
    text = (d / "Dockerfile").read_text()
    split = I.split_files(family, MODELS)
    stages = {I._stage(f) for f in split}
    instructions = [ln.split()[0] for ln in text.splitlines()
                    if ln and not ln.startswith(("#", " ")) and not ln.startswith("\t")]
    assert set(instructions) <= {"FROM", "LABEL", "RUN", "COPY", "CMD"}   # no ADD/ARG/ENV/USER
    copies = re.findall(r"^COPY (.*)$", text, re.M)
    for copy_args in copies:   # docker/extras (licences, handler, joiner) or a split stage's parts
        m = re.fullmatch(r"--from=(split-[\w-]+) (/comfyui/models/\S+\.kdp-part\d|/kdp-split/\w+\.json) \2",
                         copy_args)
        if m:
            assert m.group(1) in stages, copy_args
            continue
        assert re.fullmatch(r"--from=extras (licenses/[\w.-]+ /licenses/[\w.-]+|handler\.py /handler\.py"
                            r"|assemble\.py /kdp-assemble\.py)", copy_args), copy_args
    if not MODELS["families"][family].get("handler"):
        assert "/handler.py" not in text
    assert instructions.count("FROM") == 1 + len(split)
    assert instructions.count("CMD") == (1 if split else 0)
    if split:
        assert text.rstrip().endswith('CMD ["python3", "/kdp-assemble.py", "/start.sh"]')
    froms = re.findall(r"^FROM (\S+)(?: AS (\S+))?$", text, re.M)
    assert len(froms) == instructions.count("FROM")
    for image, stage in froms:
        assert re.fullmatch(r"runpod/worker-comfyui:5\.10\.0-base@sha256:[0-9a-f]{64}", image)
    assert [s for _, s in froms] == sorted(stages) + [""] or [s for _, s in froms][-1] == ""
    assert not re.search(r"token|secret|password|HF_TOKEN", text, re.I)
    assert (d / ".dockerignore").read_text().splitlines()[-2:] == ["*", "!Dockerfile"]
    assert sorted(p.name for p in d.iterdir()) == [".dockerignore", "Dockerfile"]
    assert sorted(p.name for p in (I.DOCKER / "fetch").iterdir() if p.name != "__pycache__") == ["fetch.py", "tool.py"]
    tool_runs = text.count("python3 /tmp/kdp-tool.py ")
    assert tool_runs == len(MODELS["families"][family].get("tools", []))
    assert sorted(p.name for p in I.EXTRAS.iterdir() if p.name != "__pycache__") == [
        "assemble.py", "handler.py", "licenses"]


def test_fetcher_reads_no_token():
    src = (I.DOCKER / "fetch" / "fetch.py").read_text()
    assert "environ" in src and not re.search(r"TOKEN|Authorization", src)


def test_workflow_builds_scans_and_pushes():
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf[True]   # PyYAML reads the `on:` key as True
    assert on["workflow_dispatch"]["inputs"]["family"]["options"] == ["all", *FAMILIES]
    assert on["push"]["branches"] == ["main"]
    assert "docker/**" in on["push"]["paths"]
    build = wf["jobs"]["build"]
    assert build["permissions"] == {"contents": "read", "packages": "write"}
    assert wf["permissions"] == {"contents": "read"}
    names = [s.get("name", s.get("uses", "")) for s in build["steps"]]
    order = [next(i for i, n in enumerate(names) if key in n)
             for key in ("Free runner disk", "Build", "Review manifest", "Secret scan", "Push")]
    assert order == sorted(order)   # scan before push: a finding never reaches the registry
    scan = next(s for s in build["steps"] if s.get("name", "").startswith("Secret scan"))
    assert "--exit-code 1" in scan["run"]
    assert re.fullmatch(r"ghcr\.io/gitleaks/gitleaks@sha256:[0-9a-f]{64}", build["env"]["GITLEAKS"])
    upload = next(s for s in build["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["name"] == "image-manifest-${{ matrix.family }}"
    text = WORKFLOW.read_text()
    for uses in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), uses   # pinned by SHA
    assert "secrets.GITHUB_TOKEN" in text and "build-arg" not in text and "--secret" not in text
    assert "kdp-comfy-${{ matrix.family }}" in text and ":latest" in text


def test_images_point_at_this_repository():
    for family in FAMILIES:
        text = (I.DOCKER / family / "Dockerfile").read_text()
        assert 'org.opencontainers.image.source="https://github.com/darkrsa/comfy-images"' in text
    assert I.image_name("flux2klein") == "ghcr.io/darkrsa/kdp-comfy-flux2klein"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def plan_repo(tmp_path):
    """A throwaway git repo with this repo's images.py, models.yaml, docker/ and ci/."""
    for name in ("images.py", "models.yaml"):
        shutil.copy(HERE / name, tmp_path / name)
    for name in ("docker", "ci"):
        shutil.copytree(HERE / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return tmp_path


def _plan_after(repo, path):
    before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    target = repo / path
    target.write_text(target.read_text() + "\n# touched\n")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "change")
    out = subprocess.run(["bash", "ci/plan_families.sh", before, "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True)
    return out.stdout.split()


@pytest.mark.parametrize(("path", "families"), [
    ("docker/zimage-full/Dockerfile", ["zimage-full"]),          # not the last family: used to exit 1
    ("docker/trellis2/Dockerfile", ["trellis2"]),
    ("docker/extras/handler.py", ["wan22-5b", "trellis2", "trellis2-pod", "wan22-s2v", "wan21-infinitetalk"]),      # only families that copy extras
    ("docker/fetch/fetch.py", FAMILIES),
    ("README.md", []),
])
def test_plan_rebuilds_only_what_changed(plan_repo, path, families):
    (plan_repo / "README.md").write_text("readme\n")
    _git(plan_repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "README.md")
    _git(plan_repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "readme")
    assert _plan_after(plan_repo, path) == families


def test_plan_with_unknown_base_rebuilds_all(plan_repo):
    out = subprocess.run(["bash", "ci/plan_families.sh", "0" * 40, "HEAD"], cwd=plan_repo, check=True,
                         capture_output=True, text=True)
    assert out.stdout.split() == FAMILIES


def test_split_files_ship_every_part_and_size_the_disk():
    for family in FAMILIES:
        text = (I.DOCKER / family / "Dockerfile").read_text()
        split = I.split_files(family, MODELS)
        assert I.container_disk_gb(family, MODELS) == I.CONTAINER_DISK_GB + (
            int(sum(f["size_gb"] for f in split)) + 1 if split else 0)
        for f in split:
            assert f["size_gb"] / f["parts"] <= I.LAYER_CAP_GB
            for i in range(f["parts"]):
                part = f"/comfyui/{f['path']}.kdp-part{i}"
                assert f"COPY --from={I._stage(f)} {part} {part}\n" in text
            assert f"COPY --from={I._stage(f)} /kdp-split/{f['name']}.json /kdp-split/{f['name']}.json" in text
    assert {f for f in FAMILIES if I.split_files(f, MODELS)} == {"wan22-s2v", "wan21-infinitetalk"}


def test_split_needs_enough_parts():
    models = _models_with(wan2_2_s2v_14b_fp8={"parts": 1}, wan2_1_i2v_480p_14b_fp8={"parts": 1})
    I.validate(models)   # unsplit: allowed (bf16 files compress under the cap; CI measures)
    models = _models_with(wan2_2_s2v_14b_fp8={"parts": 0})
    with pytest.raises(I.ModelsError, match="positive integer"):
        I.validate(models)
    models = _models_with(z_image_bf16={"parts": 1, "size_gb": 30.0})
    I.validate(models)
    models = _models_with(z_image_bf16={"parts": 2, "size_gb": 30.0})
    with pytest.raises(I.ModelsError, match="per-layer budget"):
        I.validate(models)
