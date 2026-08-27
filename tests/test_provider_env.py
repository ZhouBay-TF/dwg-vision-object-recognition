from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from dwg_vision.providers import ArkVisionProvider, DeepSeekVisionProvider, _image_data_url


def test_ark_provider_reads_anthropic_compatible_aliases(monkeypatch) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARK_BASE_URL", raising=False)
    monkeypatch.delenv("ARK_VISION_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ark-test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ark.example/v1")
    monkeypatch.setenv("ANTHROPIC_MODEL", "doubao-vision-test")

    provider = ArkVisionProvider()

    assert provider.name == "ark"
    assert provider.model == "doubao-vision-test"
    assert provider._client.base_url == "https://ark.example/v1/"


def test_deepseek_provider_reads_legacy_lowercase_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("deepseek", "deepseek-test-key")

    provider = DeepSeekVisionProvider(model="deepseek-vision-test")

    assert provider.name == "deepseek"
    assert provider.model == "deepseek-vision-test"


def test_provider_request_timeout_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("ARK_API_KEY", "ark-test-key")
    monkeypatch.setenv("ARK_VISION_MODEL", "doubao-vision-test")
    monkeypatch.setenv("VISION_REQUEST_TIMEOUT_S", "45")

    provider = ArkVisionProvider()

    assert provider.request_timeout_s == 45.0
    assert provider._client.timeout == 45.0
    assert provider._client.max_retries == 0


def test_vision_payload_downscales_cad_render_in_memory(tmp_path, monkeypatch) -> None:
    source_path = tmp_path / "large.png"
    Image.new("RGB", (4759, 2703), "white").save(source_path)
    monkeypatch.setenv("VISION_MAX_IMAGE_DIM", "1600")

    data_url = _image_data_url(source_path)
    encoded = data_url.split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.size == (1600, 909)
        assert data_url.startswith("data:image/jpeg;base64,")


def test_ark_responses_file_transport_uploads_local_path_and_uses_file_id(tmp_path, monkeypatch) -> None:
    source_path = tmp_path / "scene.png"
    Image.new("RGB", (640, 360), "white").save(source_path)
    monkeypatch.setenv("ARK_API_KEY", "ark-test-key")
    monkeypatch.setenv("ARK_VISION_MODEL", "doubao-seed-evolving")
    monkeypatch.setenv("ARK_VISION_TRANSPORT", "responses_file")

    provider = ArkVisionProvider()

    class FakeFiles:
        def __init__(self) -> None:
            self.file = None
            self.purpose = None

        def create(self, *, file, purpose):
            self.file = file
            self.purpose = purpose
            assert Path(file).resolve() == source_path.resolve()
            return SimpleNamespace(id="file-test-001")

    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(output_text='{"detections": [], "notes": "ok"}')

    fake_files = FakeFiles()
    fake_responses = FakeResponses()
    provider._client.files = fake_files
    provider._client.responses = fake_responses

    result = provider.analyze(source_path, "请只返回 JSON")

    assert result == {"detections": [], "notes": "ok"}
    assert fake_files.purpose == "user_data"
    content = fake_responses.kwargs["input"][0]["content"]
    assert content[0] == {"type": "input_image", "file_id": "file-test-001", "detail": "high"}
    assert content[1] == {"type": "input_text", "text": "请只返回 JSON"}
    assert fake_responses.kwargs["model"] == "doubao-seed-evolving"
