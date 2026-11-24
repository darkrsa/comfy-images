"""docker/extras/assemble.py: join split weight files at worker start."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kdp_assemble", HERE / "docker" / "extras" / "assemble.py")
A = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = A
spec.loader.exec_module(A)

BLOB = bytes(range(256)) * 1000 + b"tail"


def _split(tmp_path, n=3, blob=BLOB):
    dest = tmp_path / "models" / "m.safetensors"
    dest.parent.mkdir(parents=True)
    base, extra = divmod(len(blob), n)
    parts, pos = [], 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        p = dest.with_name(f"m.safetensors.kdp-part{i}")
        p.write_bytes(blob[pos:pos + size])
        parts.append({"path": str(p), "size": size, "sha256": hashlib.sha256(blob[pos:pos + size]).hexdigest()})
        pos += size
    entry = {"path": str(dest), "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest(), "parts": parts}
    split = tmp_path / "kdp-split"
    split.mkdir()
    (split / "m.json").write_text(json.dumps({"files": [entry]}))
    return dest, entry, split


def test_joins_parts_and_hides_them(tmp_path):
    dest, entry, split = _split(tmp_path)
    assert A.entries(split) == [entry]
    assert A.assemble(entry, verify=True).startswith("joined")
    assert dest.read_bytes() == BLOB
    assert not any(Path(p["path"]).exists() for p in entry["parts"])
    assert not dest.with_name("m.safetensors.kdp-tmp").exists()
    assert A.assemble(entry).startswith("ready")   # warm restart: nothing to do


def test_wrong_part_size_is_refused(tmp_path):
    dest, entry, _ = _split(tmp_path)
    Path(entry["parts"][1]["path"]).write_bytes(b"short")
    with pytest.raises(A.AssembleError, match="not .* bytes"):
        A.assemble(entry)
    assert not dest.exists()


def test_sha_mismatch_with_verify(tmp_path):
    dest, entry, _ = _split(tmp_path)
    entry["sha256"] = "0" * 64
    with pytest.raises(A.AssembleError, match="sha256"):
        A.assemble(entry, verify=True)
    assert not dest.exists() and not dest.with_name("m.safetensors.kdp-tmp").exists()


def test_partial_dest_is_rebuilt(tmp_path):
    dest, entry, _ = _split(tmp_path)
    dest.write_bytes(b"half")
    A.assemble(entry)
    assert dest.read_bytes() == BLOB


def test_too_little_disk_fails_before_writing(tmp_path, monkeypatch):
    dest, entry, _ = _split(tmp_path)
    monkeypatch.setattr(A.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(A.AssembleError, match="container disk"):
        A.assemble(entry)
    assert not dest.exists()


def test_read_fallback_when_copy_file_range_fails(tmp_path, monkeypatch):
    dest, entry, _ = _split(tmp_path)
    def boom(*a):
        raise OSError(18, "EXDEV")
    monkeypatch.setattr(A.os, "copy_file_range", boom, raising=False)
    A.assemble(entry, verify=True)
    assert dest.read_bytes() == BLOB


def test_main_fails_without_exec_on_error(tmp_path, monkeypatch):
    _, entry, split = _split(tmp_path)
    Path(entry["parts"][0]["path"]).unlink()
    monkeypatch.setattr(A, "SPLIT_DIR", split)
    monkeypatch.setattr(A.os, "execv", lambda *a: pytest.fail("must not start ComfyUI"))
    assert A.main(["/start.sh"]) == 1


def test_main_execs_the_command(tmp_path, monkeypatch):
    dest, _, split = _split(tmp_path)
    monkeypatch.setattr(A, "SPLIT_DIR", split)
    called = []
    monkeypatch.setattr(A.os, "execv", lambda path, argv: called.append(argv))
    assert A.main([]) == 0
    assert called == [["/start.sh"]] and dest.read_bytes() == BLOB
