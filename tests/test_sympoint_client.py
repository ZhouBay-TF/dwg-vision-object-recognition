from __future__ import annotations

import io
import zipfile

from dwg_vision.sympoint_client import SymPointRemoteClient, read_result_zip


def _result_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("job_result.json", '{"status":"completed"}')
        archive.writestr(
            "scenes/scene_a/sympoint_result.json",
            '{"schema_version":"sympoint_result.v1","scene_id":"scene_a","instances":[]}',
        )
    return buffer.getvalue()


def test_read_result_zip_extracts_scene_results(tmp_path) -> None:
    path = tmp_path / "result.zip"
    path.write_bytes(_result_zip())
    result = read_result_zip(path)
    assert result["job_result"]["status"] == "completed"
    assert result["scenes"]["scene_a"]["schema_version"] == "sympoint_result.v1"


def test_client_submit_and_download_contract(tmp_path) -> None:
    bundle = tmp_path / "job_bundle.zip"
    bundle.write_bytes(b"zip-placeholder")
    client = SymPointRemoteClient()
    requests: list[tuple[str, str, bytes | None, str | None]] = []

    def fake_request(method, path, *, body=None, content_type=None, **kwargs):
        requests.append((method, path, body, content_type))
        if path == "/v1/jobs":
            return 202, {"job_id": "job_1", "status": "queued"}, {}
        return 200, _result_zip(), {"content-type": "application/zip"}

    client._request = fake_request  # type: ignore[method-assign]
    submitted = client.submit_bundle(bundle, idempotency_key="same-input")
    output = client.download_result("job_1", tmp_path / "download.zip")
    assert submitted["job_id"] == "job_1"
    assert output.is_file()
    assert requests[0][0:2] == ("POST", "/v1/jobs")
    assert b'name="bundle"' in (requests[0][2] or b"")
    assert b'name="idempotency_key"' in (requests[0][2] or b"")
