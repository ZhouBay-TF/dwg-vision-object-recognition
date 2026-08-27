from __future__ import annotations

from dwg_vision.providers import ArkVisionProvider, DeepSeekVisionProvider


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
