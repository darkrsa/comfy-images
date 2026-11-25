"""docker/fetch/fetch.py against a local stand-in for Hugging Face (no internet)."""
import hashlib
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch", HERE / "docker" / "fetch" / "fetch.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)

BLOB = b"safetensors-bytes" * 1000
SHA = hashlib.sha256(BLOB).hexdigest()


class FakeHF(BaseHTTPRequestHandler):
    licences = {"good/repo": "apache-2.0", "bad/repo": "other", "named/repo": "other",
                "mit/repo": "mit"}
    licence_names = {"named/repo": "dinov3-license"}
    blob = BLOB
    sha = SHA
    drop_first = False  # cut the first body transfer short, to exercise resume
    gets = []

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        if self.path.startswith("/resolve/"):
            self._resolve(head=True)
        else:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/models/"):
            repo = self.path[len("/api/models/"):]
            card = {"license": self.licences.get(repo, "unknown")}
            if repo in self.licence_names:
                card["license_name"] = self.licence_names[repo]
            body = json.dumps({"cardData": card}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/resolve/"):
            self._resolve(head=False)
        elif self.path.startswith("/cdn/"):
            self._cdn()
        else:
            self.send_error(404)

    def _resolve(self, head):
        if "missing" in self.path:
            self.send_error(404)
            return
        if not self.path.startswith("/resolve/cache/"):   # HF's same-host hop first
            self.send_response(307)
            self.send_header("Location", "/resolve/cache/" + self.path[len("/resolve/"):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "/cdn/file")
        self.send_header("x-linked-size", str(len(self.blob)))
        self.send_header("x-linked-etag", f'"{self.sha}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cdn(self):
        start, end = 0, len(self.blob) - 1
        rng = self.headers.get("Range")
        type(self).gets.append(rng)
        if rng:
            a, _, b = rng.split("=")[1].partition("-")
            start, end = int(a), int(b) if b else end
            self.send_response(206)
        else:
            self.send_response(200)
        body = self.blob[start:end + 1]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if type(self).drop_first and rng != "bytes=0-0":
            type(self).drop_first = False
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(body)


@pytest.fixture
def hf(monkeypatch, tmp_path):
    FakeHF.gets = []
    FakeHF.drop_first = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(fetch, "HF", base)
    monkeypatch.setattr(fetch, "MANIFEST", tmp_path / "kdp-models.json")
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    yield base
    server.shutdown()


def test_downloads_verifies_and_records(hf, tmp_path, capsys):
    root = tmp_path / "comfyui"
    assert fetch.main(["--root", str(root), "models/unet/m.safetensors",
                       f"{hf}/resolve/m.safetensors", "-", "good/repo", "apache-2.0"]) == 0
    dest = root / "models/unet/m.safetensors"
    assert dest.read_bytes() == BLOB
    assert not dest.with_name("m.safetensors.part").exists()
    assert f"verified models/unet/m.safetensors {len(BLOB)} {SHA}" in capsys.readouterr().out
    entry = json.loads((tmp_path / "kdp-models.json").read_text())["files"][0]
    assert entry == {"path": str(dest), "url": f"{hf}/resolve/m.safetensors", "size": len(BLOB),
                     "sha256": SHA, "licence": "apache-2.0", "licence_repo": "good/repo"}


def test_pinned_sha_must_match(hf, tmp_path, capsys):
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "0" * 64, "good/repo", "apache-2.0"])
    assert rc == 1
    assert "!= pinned" in capsys.readouterr().err
    assert not (tmp_path / "a.safetensors").exists()


def test_refuses_a_licence_other_than_declared(hf, tmp_path, capsys):
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "-", "bad/repo",
                     "apache-2.0"])
    assert rc == 1
    assert "licence is 'other', expected 'apache-2.0'" in capsys.readouterr().err
    assert FakeHF.gets == []  # nothing downloaded


def test_refuses_when_declared_licence_differs_from_an_approved_card(hf, tmp_path, capsys):
    # The card says mit; models.yaml claimed apache-2.0: the mismatch is refused, not trusted.
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "-", "mit/repo",
                     "apache-2.0"])
    assert rc == 1
    assert "licence is 'mit', expected 'apache-2.0'" in capsys.readouterr().err
    assert FakeHF.gets == []


def test_custom_licence_is_matched_by_its_name(hf, tmp_path):
    assert fetch.licence_of("named/repo") == "dinov3-license"
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "-", "named/repo",
                     "dinov3-license"])
    assert rc == 0
    assert (tmp_path / "a.safetensors").read_bytes() == BLOB


def test_bare_other_is_not_a_licence_name(hf, tmp_path, capsys):
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "-", "bad/repo",
                     "dinov3-license"])
    assert rc == 1
    assert "licence is 'other'" in capsys.readouterr().err


def test_follows_same_host_hops_but_not_the_cdn(hf, monkeypatch):
    assert fetch.remote_meta(f"{hf}/resolve/a") == (len(BLOB), SHA)
    with pytest.raises(fetch.FetchError, match="hops: 200"):
        fetch.remote_meta(f"{hf}/cdn/file")   # a 200 without LFS headers


def test_missing_file_is_a_clear_error(hf, tmp_path, capsys):
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/missing", "-", "good/repo", "apache-2.0"])
    assert rc == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_corrupt_download_is_deleted(hf, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(FakeHF, "blob", b"x" * len(BLOB))  # same size, wrong bytes
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "-", "good/repo", "apache-2.0"])
    assert rc == 1
    assert "sha256" in capsys.readouterr().err
    assert list(tmp_path.glob("a.safetensors*")) == []


def test_interrupted_download_resumes_within_its_range(hf, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "PARTS", 1)
    FakeHF.drop_first = True
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "-", "good/repo", "apache-2.0"])
    assert rc == 0
    assert (tmp_path / "a.safetensors").read_bytes() == BLOB
    first, second = FakeHF.gets[1:3]
    assert FakeHF.gets[0] == "bytes=0-0"   # resolving the CDN URL once
    assert first == f"bytes=0-{len(BLOB) - 1}" and second != first and second.startswith("bytes=")


def test_parallel_ranges_reassemble_the_file(hf, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "RANGE", 1000)   # 17 ranges over 17 kB
    monkeypatch.setattr(fetch, "PARTS", 4)
    rc = fetch.main(["--root", str(tmp_path), "a.safetensors", f"{hf}/resolve/a", "-", "good/repo", "apache-2.0"])
    assert rc == 0
    assert (tmp_path / "a.safetensors").read_bytes() == BLOB
    assert len(FakeHF.gets) == 1 + 17


def test_parts_split_the_verified_file(hf, tmp_path, capsys):
    root = tmp_path / "comfyui"
    assert fetch.main(["--root", str(root), "--parts", "3", "models/unet/m.safetensors",
                       f"{hf}/resolve/m.safetensors", "-", "good/repo", "apache-2.0"]) == 0
    dest = root / "models/unet/m.safetensors"
    assert not dest.exists()   # only the parts go into layers
    entry = json.loads((tmp_path / "kdp-models.json").read_text())["files"][0]
    assert entry["sha256"] == SHA and entry["size"] == len(BLOB)
    parts = entry["parts"]
    assert [p["path"] for p in parts] == [f"{dest}.kdp-part{i}" for i in range(3)]
    joined = b"".join(open(p["path"], "rb").read() for p in parts)
    assert joined == BLOB
    assert max(p["size"] for p in parts) - min(p["size"] for p in parts) <= 1
    assert all(p["sha256"] == hashlib.sha256(open(p["path"], "rb").read()).hexdigest() for p in parts)


def test_split_refuses_nonsense(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"ab")
    with pytest.raises(fetch.FetchError):
        fetch.split(f, 1)
    with pytest.raises(fetch.FetchError):
        fetch.split(f, 3)


def test_split_manifest_goes_to_a_new_directory(hf, tmp_path, monkeypatch):
    # The split build stage writes KDP_MANIFEST=/kdp-split/<name>.json; /kdp-split/ does not exist
    # in the base image (CI run 45558735044 failed here after download + split).
    monkeypatch.setattr(fetch, "MANIFEST", tmp_path / "kdp-split" / "m.json")
    root = tmp_path / "comfyui"
    assert fetch.main(["--root", str(root), "--parts", "2", "models/unet/m.safetensors",
                       f"{hf}/resolve/m.safetensors", "-", "good/repo", "apache-2.0"]) == 0
    entry = json.loads((tmp_path / "kdp-split" / "m.json").read_text())["files"][0]
    assert len(entry["parts"]) == 2
