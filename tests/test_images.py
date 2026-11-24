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
    assert FAMILIES == ["zimage-full", "zimage-turbo", "flux2klein", "zimage-turbo-cn", "wan22-5b", "trellis2"]
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


def test_committed_dockerfiles_are_rendered_from_models_yaml():
    assert I.stale() == []


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_file_is_fetched_with_sha256_check(family):
    text = (I.DOCKER / family / "Dockerfile").read_text()
    runs = re.findall(r"^RUN (.*?)(?=^\s*$|^#|\Z)", text, re.S | re.M)
    files = I.family_files(family, MODELS)
    assert len(runs) == len(files)   # one layer per weight file
    for f, run in zip(files, runs):
        args = run.replace("\\\n", " ").split()
        assert "--mount=type=bind,from=fetch,source=fetch.py,target=/tmp/kdp-fetch.py" in args
        i = args.index("/tmp/kdp-fetch.py")
        assert args[i + 1:] == ["--root", "/comfyui", f["path"], f["url"],
                                f.get("sha256") or "-", f["licence_repo"], f["licence"]]
        assert f["url"].startswith("https://huggingface.co/")
        assert f["licence"] in MODELS["approved_licences"]


@pytest.mark.parametrize("family", FAMILIES)
def test_image_adds_nothing_but_weights_licences_and_handler(family):
    d = I.DOCKER / family
    text = (d / "Dockerfile").read_text()
    instructions = [ln.split()[0] for ln in text.splitlines()
                    if ln and not ln.startswith(("#", " ")) and not ln.startswith("\t")]
    assert set(instructions) <= {"FROM", "LABEL", "RUN", "COPY"}   # no ADD/ARG/ENV/USER
    copies = re.findall(r"^COPY (.*)$", text, re.M)
    for copy_args in copies:   # only from docker/extras: licence texts and the handler
        assert re.fullmatch(r"--from=extras (licenses/[\w.-]+ /licenses/[\w.-]+|handler\.py /handler\.py)",
                            copy_args), copy_args
    if not MODELS["families"][family].get("handler"):
        assert "/handler.py" not in text
    assert instructions.count("FROM") == 1
    assert re.search(r"^FROM runpod/worker-comfyui:5\.10\.0-base@sha256:[0-9a-f]{64}$", text, re.M)
    assert not re.search(r"token|secret|password|HF_TOKEN", text, re.I)
    assert (d / ".dockerignore").read_text().splitlines()[-2:] == ["*", "!Dockerfile"]
    assert sorted(p.name for p in d.iterdir()) == [".dockerignore", "Dockerfile"]
    assert sorted(p.name for p in (I.DOCKER / "fetch").iterdir() if p.name != "__pycache__") == ["fetch.py"]
    assert sorted(p.name for p in I.EXTRAS.iterdir() if p.name != "__pycache__") == ["handler.py", "licenses"]


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
    ("docker/extras/handler.py", ["wan22-5b", "trellis2"]),      # only families that copy extras
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
