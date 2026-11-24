"""docker/extras/handler.py against a local stand-in for the ComfyUI HTTP API (no GPU, no network)."""
import base64
import importlib.util
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("comfy_handler", HERE / "docker" / "extras" / "handler.py")
H = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = H   # dataclasses look their module up while the class is created
spec.loader.exec_module(H)

GLB = b"glTF" + b"\x00" * 3000   # large enough that base64 growth (~1 kB) dwarfs JSON overhead
PNG = b"\x89PNG" + b"\x01" * 50
WORKFLOW = {"1": {"class_type": "LoadImage", "inputs": {"image": "zaffy.png"}}}


class FakeComfy(BaseHTTPRequestHandler):
    """Records uploads and prompts; answers history with a SaveGLB-style `3d` output."""
    uploads: list = []
    prompts: list = []
    history_polls = 0
    status = {"status_str": "success", "completed": True, "messages": []}
    outputs = {
        "9": {"3d": [{"filename": "mesh_00001_.glb", "subfolder": "3d", "type": "output"}]},
        "12": {"images": [{"filename": "preview.png", "subfolder": "", "type": "temp"}]},
        "10": {"images": [{"filename": "front.png", "subfolder": "", "type": "output"}],
               "text": ["not a file list of dicts"]},
    }
    files = {("3d", "mesh_00001_.glb"): GLB, ("", "front.png"): PNG}
    prompt_status = 200
    node_errors: dict = {}
    history_failures = 0   # answer this many /history polls with HTTP 503 first
    cancels: list = []

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/system_stats":
            self._json(200, {"system": {}})
        elif url.path.startswith("/history/"):
            if type(self).history_failures > 0:
                type(self).history_failures -= 1
                self.send_error(503)
                return
            type(self).history_polls += 1
            prompt_id = url.path.rsplit("/", 1)[1]
            if type(self).history_polls < 2:   # first poll: still running
                self._json(200, {})
            else:
                self._json(200, {prompt_id: {"status": self.status, "outputs": self.outputs}})
        elif url.path == "/view":
            q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            assert q["type"] == ["output"]
            content = self.files.get((q["subfolder"][0], q["filename"][0]))
            if content is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/upload/image":
            type(self).uploads.append((self.headers["Content-Type"], body))
            self._json(200, {"name": "zaffy.png"})
        elif self.path == "/prompt":
            type(self).prompts.append(json.loads(body))
            if self.prompt_status != 200:
                self._json(self.prompt_status, {"error": "bad node", "node_errors": {"1": "missing"}})
            else:
                self._json(200, {"prompt_id": "p-1", "number": 0, "node_errors": self.node_errors})
        elif self.path in ("/queue", "/interrupt"):
            type(self).cancels.append((self.path, json.loads(body)))
            self._json(200, {})
        else:
            self.send_error(404)


@pytest.fixture
def comfy(monkeypatch):
    FakeComfy.uploads, FakeComfy.prompts, FakeComfy.history_polls = [], [], 0
    FakeComfy.cancels, FakeComfy.history_failures = [], 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(H, "COMFY_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(H, "POLL_S", 0.01)
    monkeypatch.setattr(H, "JOB_TIMEOUT_S", 5.0)   # a hang in a test fails instead of waiting 30 min
    monkeypatch.setattr(H, "STARTUP_TIMEOUT_S", 5.0)
    yield FakeComfy
    server.shutdown()


def _job(**input_fields):
    return {"id": "job-1", "input": {"workflow": WORKFLOW, **input_fields}}


def test_returns_3d_and_image_outputs_but_not_previews(comfy):
    image = base64.b64encode(PNG).decode()
    result = H.handler(_job(images=[{"name": "zaffy.png", "image": f"data:image/png;base64,{image}"}]))
    assert "error" not in result, result
    files = result["files"]
    assert [(f["node"], f["kind"], f["filename"]) for f in files] == [
        ("9", "3d", "mesh_00001_.glb"), ("10", "images", "front.png")]
    assert base64.b64decode(files[0]["base64"]) == GLB and files[0]["bytes"] == len(GLB)
    assert base64.b64decode(files[1]["base64"]) == PNG
    assert comfy.prompts[0]["prompt"] == WORKFLOW
    content_type, body = comfy.uploads[0]
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'filename="zaffy.png"' in body and PNG in body


def test_failed_execution_is_a_failed_job(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "status", {
        "status_str": "error", "completed": False,
        "messages": [["execution_start", {}], ["execution_error", {"node_id": "4", "exception_message": "OOM"}]]})
    result = H.handler(_job())
    assert result["error"].startswith("ComfyError: workflow failed")
    assert "OOM" in result["error"]


def test_refused_workflow_reports_comfy_detail(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "prompt_status", 400)
    result = H.handler(_job())
    assert "HTTP 400" in result["error"] and "bad node" in result["error"]


def test_no_saved_output_is_an_error_not_an_empty_success(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "outputs", {"12": FakeComfy.outputs["12"]})
    result = H.handler(_job())
    assert result == {"error": "ComfyError: workflow finished but saved no output file (is there a Save* node?)"}


def _result_size(result):
    return len(json.dumps(result))


def test_result_over_the_cap_fails_with_the_file_named(comfy, monkeypatch):
    monkeypatch.setattr(H, "MAX_RESULT_BYTES", H.encoded_size(len(GLB)))   # no room for JSON
    result = H.handler(_job())
    assert result["error"].startswith("OutputTooLargeError: result would exceed")
    assert "mesh_00001_.glb" in result["error"]


def test_cap_counts_the_encoded_result_not_raw_bytes(comfy, monkeypatch):
    # One file whose raw bytes fit and whose base64 does not: must fail (the transport limit
    # applies to what is sent).
    monkeypatch.setattr(FakeComfy, "outputs", {"9": FakeComfy.outputs["9"]})
    full = _result_size(H.handler(_job()))
    base64_growth = H.encoded_size(len(GLB)) - len(GLB)
    monkeypatch.setattr(H, "MAX_RESULT_BYTES", full - base64_growth // 2)   # raw-byte count would fit
    assert H.handler(_job())["error"].startswith("OutputTooLargeError")


def test_result_under_the_cap_passes_and_respects_it(comfy, monkeypatch):
    monkeypatch.setattr(H, "MAX_RESULT_BYTES", 10_000)
    result = H.handler(_job())
    assert len(result["files"]) == 2
    assert _result_size(result) <= 10_000


def test_invalid_node_fails_the_job_even_when_comfy_queues_the_rest(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "node_errors", {"9": {"errors": [{"message": "bad mesh input"}]}})
    result = H.handler(_job())
    assert result["error"].startswith("ComfyError: workflow has invalid nodes")
    assert "bad mesh input" in result["error"]
    assert ("/queue", {"delete": ["p-1"]}) in comfy.cancels


def test_timeout_cancels_the_prompt(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "status", {"status_str": "success", "completed": False})
    monkeypatch.setattr(H, "JOB_TIMEOUT_S", 0.2)
    result = H.handler(_job())
    assert "did not finish" in result["error"]
    assert ("/queue", {"delete": ["p-1"]}) in comfy.cancels
    assert ("/interrupt", {"prompt_id": "p-1"}) in comfy.cancels


def test_interrupted_prompt_reports_its_message(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "status", {
        "status_str": "error", "completed": False,
        "messages": [["execution_start", {}], ["execution_interrupted", {"node_id": "7"}]]})
    result = H.handler(_job())
    assert "execution_interrupted" in result["error"] and "execution_start" not in result["error"]


def test_transient_history_errors_are_retried(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "history_failures", 2)
    assert len(H.handler(_job())["files"]) == 2


def test_a_file_reported_twice_is_returned_once():
    entry = {"outputs": {"9": {"3d": [{"filename": "m.glb", "subfolder": "3d", "type": "output"}],
                               "files": [{"filename": "m.glb", "subfolder": "3d", "type": "output"}]},
                         "bad": "not a dict"}}
    assert H.output_refs(entry) == [H.OutputRef("9", "3d", "m.glb", "3d")]


@pytest.mark.parametrize(("raw", "message"), [("abc", "is not a number"), ("0", "must be > 0"),
                                              ("-1", "must be > 0"), ("nan", "must be > 0")])
def test_bad_settings_are_refused(raw, message):
    with pytest.raises(H.ConfigError, match=message):
        H.positive_setting("MAX_RESULT_MB", 15, {"MAX_RESULT_MB": raw})


def test_default_setting_is_used_when_unset():
    assert H.positive_setting("MAX_RESULT_MB", 15, {}) == 15


def test_history_timeout_is_an_error(comfy, monkeypatch):
    monkeypatch.setattr(FakeComfy, "status", {"status_str": "success", "completed": False})
    monkeypatch.setattr(H, "JOB_TIMEOUT_S", 0.2)
    with pytest.raises(H.ComfyError, match="did not finish"):
        H.wait_for_history("p-1")
    assert ("/queue", {"delete": ["p-1"]}) in comfy.cancels


def test_unreachable_comfy_times_out(monkeypatch):
    monkeypatch.setattr(H, "COMFY_URL", "http://127.0.0.1:9")   # discard port: refused
    monkeypatch.setattr(H, "POLL_S", 0.01)
    with pytest.raises(H.ComfyError, match="did not answer"):
        H.wait_for_comfy(timeout_s=0.1)


@pytest.mark.parametrize(("job_input", "message"), [
    (None, "input must be an object"),
    ({}, "input.workflow must be"),
    ({"workflow": {}}, "input.workflow must be"),
    ({"workflow": WORKFLOW, "images": {}}, "input.images must be a list"),
    ({"workflow": WORKFLOW, "images": ["x"]}, r"input.images\[0\] must be an object"),
    ({"workflow": WORKFLOW, "images": [{"name": "../x.png", "image": "QQ=="}]}, "plain file name"),
    ({"workflow": WORKFLOW, "images": [{"name": 'a".png', "image": "QQ=="}]}, "plain file name"),
    ({"workflow": WORKFLOW, "images": [{"name": ".hidden", "image": "QQ=="}]}, "plain file name"),
    ({"workflow": WORKFLOW, "images": [{"name": "a.png", "image": ""}]}, "must be a base64 string"),
    ({"workflow": WORKFLOW, "images": [{"name": "a.png", "image": "not base64!"}]}, "not valid base64"),
])
def test_malformed_input_is_refused_before_comfy_is_called(job_input, message):
    with pytest.raises(H.JobInputError, match=message):
        H.parse_input(job_input)


def test_input_error_becomes_a_failed_job_without_touching_comfy(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ComfyUI must not be called")
    monkeypatch.setattr(H, "_request", boom)
    assert H.handler({"id": "j", "input": {}})["error"].startswith("JobInputError:")
