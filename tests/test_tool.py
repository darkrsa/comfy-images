"""docker/fetch/tool.py against a local HTTP server (no internet)."""
import hashlib
import importlib.util
import io
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tool", HERE / "docker" / "fetch" / "tool.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def _archive(files: dict[str, bytes], top: str = "blender-9.9.9-linux-x64") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


ARCHIVE = _archive({"blender": b"#!/bin/sh\necho blender\n", "4.5/scripts/x.py": b"print(1)\n"})
SHA = hashlib.sha256(ARCHIVE).hexdigest()


class Server(BaseHTTPRequestHandler):
    body = ARCHIVE
    cut_first = False
    ignore_range = False
    forbid = ""   # path prefix answered with 403 (download.blender.org to CI runners)
    gets: list[str] = []
    paths: list[str] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).paths.append(self.path)
        if self.forbid and self.path.startswith(self.forbid):
            self.send_error(403)
            return
        type(self).gets.append(self.headers.get("Range") or "")
        start = 0
        rng = self.headers.get("Range")
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
        data = self.body[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.cut_first and len(type(self).gets) == 1:
            self.wfile.write(data[: len(data) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(data)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the manifest and the /usr/local/bin link at tmp_path."""
    monkeypatch.setattr(tool, "MANIFEST", tmp_path / "kdp-tools.json")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_path = tool.Path

    def fake_path(p, *rest):
        if str(p) == "/usr/local/bin":
            return bin_dir
        return real_path(p, *rest)

    monkeypatch.setattr(tool, "Path", fake_path)
    return tmp_path


def _run(port, sandbox, sha=SHA, size=None):
    url = f"http://127.0.0.1:{port}/blender-9.9.9-linux-x64.tar.xz"
    return tool.main(["blender", "9.9.9", url, sha, str(size or len(ARCHIVE)),
                      str(sandbox / "opt" / "blender"), "blender"])


@pytest.fixture
def http_ok(monkeypatch):
    real = tool.install

    def install(name, version, url, *a):
        if "http://127.0.0.1" in url:
            url = url.replace("http://127.0.0.1", "https://127.0.0.1")
            monkeypatch.setattr(tool.urllib.request, "urlopen", _rewrite(tool.urllib.request.urlopen))
        return real(name, version, url, *a)

    monkeypatch.setattr(tool, "install", install)


def _rewrite(urlopen):
    def wrapped(req, *a, **kw):
        req.full_url = req.full_url.replace("https://", "http://", 1)
        return urlopen(req, *a, **kw)
    return wrapped


@pytest.fixture
def httpd():
    Server.body, Server.cut_first, Server.ignore_range, Server.gets = ARCHIVE, False, False, []
    Server.forbid, Server.paths = "", []
    s = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()


def test_installs_verified_archive_strips_top_folder_and_links_binary(httpd, sandbox, http_ok, monkeypatch):
    monkeypatch.setattr(tool.time, "sleep", lambda s: None)
    assert _run(httpd.server_port, sandbox) == 0
    dest = sandbox / "opt" / "blender"
    assert (dest / "blender").read_bytes().startswith(b"#!/bin/sh")
    assert (dest / "4.5" / "scripts" / "x.py").is_file()
    link = sandbox / "bin" / "blender"
    assert link.is_symlink() and link.resolve() == (dest / "blender").resolve()
    (entry,) = json.loads((sandbox / "kdp-tools.json").read_text())
    assert entry["name"] == "blender" and entry["sha256"] == SHA and entry["size"] == len(ARCHIVE)
    assert entry["path"] == str(dest)


def test_resumes_a_cut_download(httpd, sandbox, http_ok, monkeypatch):
    monkeypatch.setattr(tool.time, "sleep", lambda s: None)
    Server.cut_first = True
    assert _run(httpd.server_port, sandbox) == 0
    assert len(Server.gets) == 2 and Server.gets[1].startswith("bytes=")


def test_server_ignoring_range_restarts_cleanly(httpd, sandbox, http_ok, monkeypatch):
    monkeypatch.setattr(tool.time, "sleep", lambda s: None)
    Server.cut_first, Server.ignore_range = True, True
    assert _run(httpd.server_port, sandbox) == 0
    assert (sandbox / "opt" / "blender" / "blender").is_file()


def test_wrong_sha_is_refused_and_nothing_installed(httpd, sandbox, http_ok, capsys):
    assert _run(httpd.server_port, sandbox, sha="0" * 64) == 1
    assert "sha256" in capsys.readouterr().err
    assert not (sandbox / "opt").exists()
    assert not (sandbox / "kdp-tools.json").exists()


def test_wrong_size_is_refused(httpd, sandbox, http_ok, monkeypatch, capsys):
    monkeypatch.setattr(tool.time, "sleep", lambda s: None)
    assert _run(httpd.server_port, sandbox, size=len(ARCHIVE) + 1) == 1
    assert "want" in capsys.readouterr().err


def test_plain_http_is_refused(sandbox, capsys):
    assert tool.main(["blender", "1", "http://example.invalid/b.tar.xz", SHA, "1",
                      str(sandbox / "opt"), "blender"]) == 1
    assert "https URLs only" in capsys.readouterr().err


def test_archive_without_single_top_folder_is_refused(httpd, sandbox, http_ok):
    Server.body = _archive({"blender": b"x", "other": b"y"}, top="")
    sha = hashlib.sha256(Server.body).hexdigest()
    url = f"http://127.0.0.1:{httpd.server_port}/b.tar.xz"
    assert tool.main(["blender", "1", url, sha, str(len(Server.body)), str(sandbox / "opt"), "blender"]) == 1


def test_refused_url_falls_back_to_the_next_mirror_at_once(httpd, sandbox, http_ok, monkeypatch, capsys):
    sleeps = []
    monkeypatch.setattr(tool.time, "sleep", sleeps.append)
    Server.forbid = "/origin/"
    base = f"http://127.0.0.1:{httpd.server_port}"
    urls = f"{base}/origin/b.tar.xz,{base}/mirror/b.tar.xz"
    assert tool.main(["blender", "9.9.9", urls, SHA, str(len(ARCHIVE)), str(sandbox / "opt" / "blender"), "blender"]) == 0
    assert Server.paths == ["/origin/b.tar.xz", "/mirror/b.tar.xz"] and sleeps == []
    (entry,) = json.loads((sandbox / "kdp-tools.json").read_text())
    assert entry["url"] == f"https://127.0.0.1:{httpd.server_port}/mirror/b.tar.xz"
    assert "downloaded from" in capsys.readouterr().out


def test_every_url_refused_fails_the_build(httpd, sandbox, http_ok, capsys):
    Server.forbid = "/"
    base = f"http://127.0.0.1:{httpd.server_port}"
    urls = f"{base}/a/b.tar.xz,{base}/c/b.tar.xz"
    assert tool.main(["blender", "1", urls, SHA, str(len(ARCHIVE)), str(sandbox / "opt"), "blender"]) == 1
    assert "got 0 bytes" in capsys.readouterr().err and not (sandbox / "opt").exists()


def test_a_plain_http_mirror_is_refused(sandbox, capsys):
    urls = "https://example.invalid/b.tar.xz,http://mirror.invalid/b.tar.xz"
    assert tool.main(["blender", "1", urls, SHA, "1", str(sandbox / "opt"), "blender"]) == 1
    assert "https URLs only" in capsys.readouterr().err


def test_reads_no_token():
    src = (HERE / "docker" / "fetch" / "tool.py").read_text()
    assert "TOKEN" not in src and "Authorization" not in src and "environ.get(\"HF" not in src
