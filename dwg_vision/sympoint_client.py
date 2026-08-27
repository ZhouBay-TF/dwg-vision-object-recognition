from __future__ import annotations

"""Small dependency-light client for the remote SymPointV2 API."""

import json
import mimetypes
import time
import uuid
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


class SymPointRemoteError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _json_or_text(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return data.decode("utf-8", errors="replace")


def _multipart_bundle(bundle_path: Path, *, idempotency_key: str | None = None) -> tuple[bytes, str]:
    boundary = f"----dwgvision-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])

    if idempotency_key:
        field("idempotency_key", idempotency_key)
    content_type = mimetypes.guess_type(bundle_path.name)[0] or "application/zip"
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="bundle"; filename="{bundle_path.name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        bundle_path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class SymPointRemoteClient:
    """HTTP client used through an SSH/VSCode local port forward."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", *, timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout_s: float | None = None,
        raw_response: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method.upper(),
            headers={
                "Accept": "application/json",
                **({"Content-Type": content_type} if content_type else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s or self.timeout_s) as response:
                raw = response.read()
                payload = raw if raw_response else _json_or_text(raw)
                return int(response.status), payload, dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = _json_or_text(raw)
            message = payload.get("detail", payload) if isinstance(payload, dict) else payload
            raise SymPointRemoteError(
                f"SymPointV2 API {method.upper()} {path} failed ({exc.code}): {message}",
                status_code=exc.code,
                payload=payload,
            ) from exc
        except urllib.error.URLError as exc:
            raise SymPointRemoteError(f"无法连接 SymPointV2 API {self.base_url}: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        _, payload, _ = self._request("GET", "/healthz")
        return dict(payload)

    def ready(self) -> dict[str, Any]:
        _, payload, _ = self._request("GET", "/readyz")
        return dict(payload)

    def submit_bundle(self, bundle_path: Path, *, idempotency_key: str | None = None) -> dict[str, Any]:
        bundle_path = Path(bundle_path)
        if not bundle_path.is_file():
            raise FileNotFoundError(bundle_path)
        body, content_type = _multipart_bundle(bundle_path, idempotency_key=idempotency_key)
        _, payload, _ = self._request("POST", "/v1/jobs", body=body, content_type=content_type)
        if not isinstance(payload, dict) or not payload.get("job_id"):
            raise SymPointRemoteError(f"SymPointV2 API returned an invalid submit response: {payload!r}", payload=payload)
        return payload

    def get_job(self, job_id: str) -> dict[str, Any]:
        _, payload, _ = self._request("GET", f"/v1/jobs/{job_id}")
        return dict(payload)

    def wait_job(
        self,
        job_id: str,
        *,
        timeout_s: float = 1800.0,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(timeout_s)
        terminal = {"completed", "partial_failed", "failed", "cancelled"}
        while True:
            status = self.get_job(job_id)
            if status.get("status") in terminal:
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(f"SymPointV2 Job {job_id} did not finish within {timeout_s:g}s")
            time.sleep(max(0.05, float(poll_interval_s)))

    def download_result(self, job_id: str, output_path: Path) -> Path:
        _, payload, _ = self._request("GET", f"/v1/jobs/{job_id}/result", raw_response=True)
        if not isinstance(payload, (bytes, bytearray)):
            # _request decodes JSON responses; a result ZIP is returned as text
            # only when the server violates its contract.
            raise SymPointRemoteError(f"result endpoint did not return a ZIP for Job {job_id}", payload=payload)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(bytes(payload))
        return output_path

    def infer_bundle(
        self,
        bundle_path: Path,
        output_dir: Path,
        *,
        idempotency_key: str | None = None,
        timeout_s: float = 1800.0,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        submitted = self.submit_bundle(bundle_path, idempotency_key=idempotency_key)
        job_id = str(submitted["job_id"])
        status = self.wait_job(job_id, timeout_s=timeout_s, poll_interval_s=poll_interval_s)
        result_path = Path(output_dir) / f"{job_id}.zip"
        self.download_result(job_id, result_path)
        return {
            "job_id": job_id,
            "submit": submitted,
            "status": status,
            "result_zip": str(result_path),
            "result": read_result_zip(result_path),
        }


def read_result_zip(path: Path) -> dict[str, Any]:
    """Read the stable JSON files from the cloud result archive."""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        job_result: dict[str, Any] = {}
        if "job_result.json" in names:
            job_result = json.loads(archive.read("job_result.json"))
        scenes: dict[str, dict[str, Any]] = {}
        for name in sorted(names):
            if not name.endswith("/sympoint_result.json"):
                continue
            scene_result = json.loads(archive.read(name))
            scene_id = str(scene_result.get("scene_id") or Path(name).parent.name)
            scenes[scene_id] = scene_result
        return {"job_result": job_result, "scenes": scenes, "files": sorted(names)}
