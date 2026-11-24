"""Dockerfile + build workflow lint-level checks. No Docker needed."""
import re

import pytest
import yaml

import images as I
from images import HERE, load_models

MODELS = load_models()
FAMILIES = list(MODELS["families"])
WORKFLOW = HERE / ".github" / "workflows" / "build.yml"


def test_three_families_one_model_each():
    assert FAMILIES == ["zimage-full", "zimage-turbo", "flux2klein"]
    by_name = {f["name"]: f for f in MODELS["files"]}
    for fam, spec in MODELS["families"].items():
        unets = [n for n in spec["files"] if by_name[n]["path"].startswith("models/unet/")]
        assert len(unets) == 1, fam
        assert len({by_name[n]["path"] for n in spec["files"]}) == len(spec["files"])
    for prof in MODELS["profiles"].values():
        assert set(prof["files"]) == set(MODELS["families"][prof["family"]]["files"])


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
                                f.get("sha256") or "-", f["licence_repo"]]
        assert f["url"].startswith("https://huggingface.co/") and f["licence"] == "apache-2.0"


@pytest.mark.parametrize("family", FAMILIES)
def test_image_adds_nothing_but_weights(family):
    d = I.DOCKER / family
    text = (d / "Dockerfile").read_text()
    instructions = [ln.split()[0] for ln in text.splitlines()
                    if ln and not ln.startswith(("#", " ")) and not ln.startswith("\t")]
    assert set(instructions) <= {"FROM", "LABEL", "RUN"}   # no COPY/ADD/ARG/ENV/USER
    assert instructions.count("FROM") == 1
    assert re.search(r"^FROM runpod/worker-comfyui:5\.10\.0-base@sha256:[0-9a-f]{64}$", text, re.M)
    assert not re.search(r"token|secret|password|HF_TOKEN", text, re.I)
    assert (d / ".dockerignore").read_text().splitlines()[-2:] == ["*", "!Dockerfile"]
    assert sorted(p.name for p in d.iterdir()) == [".dockerignore", "Dockerfile"]
    assert sorted(p.name for p in (I.DOCKER / "fetch").iterdir() if p.name != "__pycache__") == ["fetch.py"]


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
