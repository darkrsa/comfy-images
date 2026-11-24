"""RunPod serverless handler for ComfyUI that returns every saved output file, 3D models included.

Replaces the stock runpod/worker-comfyui handler in images whose family sets `handler: outputs`
in models.yaml. The stock 5.10.0 handler only collects the `images` key of a node's UI output,
so a SaveGLB node (which reports its file under `3d`) produces a job with no result.

Job input:
    {"workflow": {<ComfyUI API-format prompt>},
     "images": [{"name": "zaffy.png", "image": "<base64, optional data: prefix>"}]}   # optional
Job output:
    {"files": [{"node": "9", "kind": "3d", "filename": "mesh_00001_.glb",
                "bytes": 1234, "base64": "<...>"}]}
A failure returns {"error": "<reason>"}, which RunPod reports as a failed job.

Only files written to ComfyUI's output folder (`type: output`) are returned; previews (`temp`)
are skipped. The started ComfyUI server is reached at COMFY_URL (default http://127.0.0.1:8188).
"""
from __future__ import annotations

import base64
import binascii
import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("comfy-outputs-handler")


class ConfigError(ValueError):
    """An environment setting is not a positive number; the worker refuses to start."""


def positive_setting(name: str, default: float, env: dict[str, str] | None = None) -> float:
    """Read a positive float from the environment; raises ConfigError for junk, zero or negative."""
    raw = (os.environ if env is None else env).get(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if not value > 0:
        raise ConfigError(f"{name}={raw!r} must be > 0")
    return value


COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
# ComfyUI loads its models before it answers; first start of a 9 GB image can take minutes.
STARTUP_TIMEOUT_S = positive_setting("COMFY_STARTUP_TIMEOUT_S", 600)
JOB_TIMEOUT_S = positive_setting("COMFY_JOB_TIMEOUT_S", 1800)
POLL_S = positive_setting("COMFY_POLL_S", 1.0)
HTTP_TIMEOUT_S = 60.0
# Cap on the result as sent: base64 of every file plus the JSON around it. RunPod job results are
# size-limited; above this the job fails with a clear message instead of an opaque transport error.
# Unmeasured against RunPod's actual limit: lower DecimateMesh's face count or the texture size,
# or raise this after measuring.
MAX_RESULT_BYTES = int(positive_setting("MAX_RESULT_MB", 15) * 1e6)
TRANSPORT_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError)
# History messages that only report progress; everything else in a failed prompt's status is shown.
PROGRESS_MESSAGES = {"execution_start", "execution_cached", "execution_success"}


class JobInputError(ValueError):
    """The job's input is malformed; the caller must fix the request."""


class ComfyError(RuntimeError):
    """ComfyUI refused the prompt, failed while running it, or did not answer in time."""


class OutputTooLargeError(RuntimeError):
    """The encoded result would exceed MAX_RESULT_BYTES."""


@dataclass(frozen=True)
class InputImage:
    name: str
    content: bytes


@dataclass(frozen=True)
class OutputRef:
    node: str
    kind: str
    filename: str
    subfolder: str


def parse_input(job_input: Any) -> tuple[dict[str, Any], list[InputImage]]:
    """Validate the job input. Returns (workflow, images); raises JobInputError."""
    if not isinstance(job_input, dict):
        raise JobInputError("input must be an object")
    workflow = job_input.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise JobInputError("input.workflow must be a non-empty ComfyUI API-format object")
    raw_images = job_input.get("images", [])
    if not isinstance(raw_images, list):
        raise JobInputError("input.images must be a list")
    images = []
    for index, entry in enumerate(raw_images):
        if not isinstance(entry, dict):
            raise JobInputError(f"input.images[{index}] must be an object")
        name, encoded = entry.get("name"), entry.get("image")
        if not isinstance(name, str) or not name or name.startswith(".") or any(c in name for c in '/\\"\r\n'):
            raise JobInputError(f"input.images[{index}].name must be a plain file name")
        if not isinstance(encoded, str) or not encoded:
            raise JobInputError(f"input.images[{index}].image must be a base64 string")
        payload = encoded.split(",", 1)[1] if encoded.startswith("data:") else encoded
        try:
            content = base64.b64decode(payload, validate=True)
        except binascii.Error as exc:
            raise JobInputError(f"input.images[{index}].image is not valid base64") from exc
        images.append(InputImage(name, content))
    return workflow, images


def _request(path: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(f"{COMFY_URL}{path}", data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        body: bytes = resp.read()
        return body


def wait_for_comfy(timeout_s: float | None = None) -> None:
    """Block until ComfyUI answers /system_stats; raises ComfyError after timeout_s
    (default STARTUP_TIMEOUT_S, read at call time)."""
    timeout_s = STARTUP_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            _request("/system_stats")
            return
        except TRANSPORT_ERRORS as exc:
            if time.monotonic() >= deadline:
                raise ComfyError(f"ComfyUI did not answer at {COMFY_URL} in {timeout_s:.0f}s: {exc}") from exc
        time.sleep(POLL_S)


def upload_image(image: InputImage) -> None:
    """POST one input image to /upload/image (multipart), overwriting a same-named file."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{image.name}"\r\n'
         "Content-Type: application/octet-stream\r\n\r\n").encode() + image.content + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    try:
        _request("/upload/image", b"".join(parts),
                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    except urllib.error.HTTPError as exc:
        raise ComfyError(f"upload of {image.name} refused: HTTP {exc.code}") from exc


def queue_prompt(workflow: dict[str, Any], client_id: str) -> str:
    """Queue the workflow; returns the prompt id. Validation errors raise ComfyError with details."""
    body = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    try:
        answer = json.loads(_request("/prompt", body, {"Content-Type": "application/json"}))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise ComfyError(f"workflow refused (HTTP {exc.code}): {detail}") from exc
    if not isinstance(answer, dict) or not isinstance(answer.get("prompt_id"), str):
        raise ComfyError(f"no prompt_id in ComfyUI answer: {str(answer)[:500]}")
    prompt_id: str = answer["prompt_id"]
    # ComfyUI queues a prompt when at least one output node validates and silently drops the
    # others; for us a dropped Save node is a failed job, not a partial result.
    if answer.get("node_errors"):
        cancel(prompt_id)
        raise ComfyError(f"workflow has invalid nodes: {json.dumps(answer['node_errors'])[:2000]}")
    return prompt_id


def cancel(prompt_id: str) -> None:
    """Best effort: drop the prompt from the queue and interrupt it if it is running, so a warm
    worker's next job does not wait behind it. Failures are logged, not raised."""
    for path, payload in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {"prompt_id": prompt_id})):
        try:
            _request(path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        except TRANSPORT_ERRORS as exc:
            log.warning("could not %s prompt %s: %s", path.strip("/"), prompt_id, exc)


def wait_for_history(prompt_id: str, timeout_s: float | None = None) -> dict[str, Any]:
    """Poll /history/<id> until the prompt finished; raises ComfyError on failure or timeout
    (default JOB_TIMEOUT_S, read at call time)."""
    timeout_s = JOB_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            history = json.loads(_request(f"/history/{urllib.parse.quote(prompt_id)}"))
        except TRANSPORT_ERRORS as exc:   # a busy server's hiccup; the deadline bounds the retries
            log.warning("history poll for %s failed, retrying: %s", prompt_id, exc)
            time.sleep(POLL_S)
            continue
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if isinstance(entry, dict):
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                messages = [m for m in status.get("messages", [])
                            if not (isinstance(m, list) and m and m[0] in PROGRESS_MESSAGES)]
                raise ComfyError(f"workflow failed: {json.dumps(messages)[:2000]}")
            if status.get("completed"):
                return entry
        time.sleep(POLL_S)
    cancel(prompt_id)
    raise ComfyError(f"workflow did not finish in {timeout_s:.0f}s (cancelled)")


def output_refs(history_entry: dict[str, Any]) -> list[OutputRef]:
    """Every saved file in the prompt's outputs, whatever key the node reports it under
    (`images`, `3d`, `gifs`, ...). Sorted by node id, then key, for a stable result order."""
    refs: list[OutputRef] = []
    seen: set[tuple[str, str]] = set()   # a file reported under two keys is returned once
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, dict):
        return refs
    for node in sorted(outputs, key=lambda node_id: (len(node_id), node_id)):
        if not isinstance(outputs[node], dict):
            continue
        for kind in sorted(outputs[node]):
            files = outputs[node][kind]
            if not isinstance(files, list):
                continue
            for item in files:
                if isinstance(item, dict) and item.get("type") == "output" and item.get("filename"):
                    ref = OutputRef(node, kind, str(item["filename"]), str(item.get("subfolder", "")))
                    if (ref.subfolder, ref.filename) not in seen:
                        seen.add((ref.subfolder, ref.filename))
                        refs.append(ref)
    return refs


def encoded_size(raw_bytes: int) -> int:
    """Length of the base64 text for raw_bytes bytes."""
    return 4 * ((raw_bytes + 2) // 3)


def read_output(ref: OutputRef, budget: int) -> bytes:
    """Fetch one output file, refusing it before download when its Content-Length already breaks
    the remaining budget (encoded bytes), and while reading when the server sends no length."""
    if budget <= 0:
        raise OutputTooLargeError(_too_large(ref, 0))
    query = urllib.parse.urlencode({"filename": ref.filename, "subfolder": ref.subfolder, "type": "output"})
    req = urllib.request.Request(f"{COMFY_URL}/view?{query}")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and encoded_size(int(length)) > budget:
            raise OutputTooLargeError(_too_large(ref, int(length)))
        content: bytes = resp.read(budget + 1)   # base64 is longer than raw: budget bounds raw too
        if encoded_size(len(content)) > budget:
            raise OutputTooLargeError(_too_large(ref, len(content)))
        return content


def _too_large(ref: OutputRef, raw_bytes: int) -> str:
    return (f"result would exceed {MAX_RESULT_BYTES / 1e6:.0f} MB at {ref.filename} "
            f"({raw_bytes / 1e6:.1f} MB raw); reduce the mesh face count or texture size")


def run(job_input: Any) -> dict[str, Any]:
    """The whole job: validate, upload, queue, wait, collect. Raises the typed errors above."""
    workflow, images = parse_input(job_input)
    wait_for_comfy()
    for image in images:
        upload_image(image)
    prompt_id = queue_prompt(workflow, uuid.uuid4().hex)
    try:
        history_entry = wait_for_history(prompt_id)
    except BaseException:
        cancel(prompt_id)   # never leave a prompt holding the GPU after the job is reported failed
        raise
    refs = output_refs(history_entry)
    if not refs:
        raise ComfyError("workflow finished but saved no output file (is there a Save* node?)")
    files: list[dict[str, Any]] = []
    used = len('{"files": []}')
    for ref in refs:
        entry = {"node": ref.node, "kind": ref.kind, "filename": ref.filename, "bytes": 0, "base64": ""}
        overhead = len(json.dumps(entry)) + 32   # keys, punctuation, the byte count's digits
        content = read_output(ref, MAX_RESULT_BYTES - used - overhead)
        encoded = base64.b64encode(content).decode()
        entry.update(bytes=len(content), base64=encoded)
        used += overhead + len(encoded)
        files.append(entry)
    return {"files": files}


def handler(job: dict[str, Any]) -> dict[str, Any]:
    """RunPod entry point: a result, or {"error": ...} for a failed job."""
    try:
        return run(job.get("input"))
    except (JobInputError, ComfyError, OutputTooLargeError) as exc:
        log.error("job %s failed: %s", job.get("id"), exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    except (*TRANSPORT_ERRORS, OSError, ValueError) as exc:   # transport or JSON trouble
        log.exception("job %s failed talking to ComfyUI", job.get("id"))
        return {"error": f"ComfyUI communication failed: {exc}"}


if __name__ == "__main__":
    import runpod  # type: ignore[import-not-found]  # provided by the worker-comfyui base image

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    runpod.serverless.start({"handler": handler})
