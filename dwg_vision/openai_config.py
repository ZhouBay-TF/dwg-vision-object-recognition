from __future__ import annotations

"""Local OpenAI-compatible Agent configuration.

The repository may use a local ``gpt.json`` written for OpenCode-style
providers. This adapter reads only the OpenAI provider's endpoint, key and
model catalog; secrets are never included in summaries or logs.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env_config import env_first


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str | None
    config_path: Path | None

    def safe_summary(self) -> dict[str, Any]:
        """Return configuration metadata without exposing the API key."""

        return {
            "provider": "openai",
            "base_url": self.base_url,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "config_path": str(self.config_path) if self.config_path else None,
            "api_key_configured": bool(self.api_key),
        }


def _candidate_config_paths(explicit_path: str | Path | None) -> list[Path]:
    values: list[Path] = []
    raw_paths = [
        explicit_path,
        env_first("GPT_CONFIG_PATH", "OPENAI_CONFIG_PATH") or None,
        Path.cwd() / "gpt.json",
        Path(__file__).resolve().parents[1] / "gpt.json",
    ]
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if path not in values:
            values.append(path)
    return values


def _load_document(explicit_path: str | Path | None) -> tuple[dict[str, Any], Path | None]:
    for path in _candidate_config_paths(explicit_path):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 GPT 配置文件：{path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"GPT 配置文件根节点必须是 JSON 对象：{path}")
        return payload, path
    return {}, None


def _openai_provider(document: dict[str, Any]) -> dict[str, Any]:
    provider = document.get("provider")
    if not isinstance(provider, dict):
        return {}
    openai = provider.get("openai")
    return openai if isinstance(openai, dict) else {}


def _select_model(models: dict[str, Any], requested: str | None) -> str:
    if requested:
        return requested
    # Prefer the explicitly small general-purpose model when it is present.
    # The fallback order avoids a silent upgrade to a large reasoning model.
    preferred = (
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5-nano",
        "gpt-4o-mini",
        "gpt-4.1-mini",
        "gpt-5.6-luna",
        "gpt-5.3-codex-spark",
        "codex-mini-latest",
    )
    for name in preferred:
        if name in models:
            return name
    if models:
        return sorted(models)[0]
    return "gpt-5.4-mini"


def load_openai_compatible_config(
    *,
    model: str | None = None,
    role: str = "agent",
    config_path: str | Path | None = None,
) -> OpenAICompatibleConfig:
    """Load a GPT endpoint from environment first, then local ``gpt.json``."""

    document, loaded_path = _load_document(config_path)
    openai = _openai_provider(document)
    options = openai.get("options") if isinstance(openai.get("options"), dict) else {}
    models = openai.get("models") if isinstance(openai.get("models"), dict) else {}

    api_key = env_first("OPENAI_API_KEY", "GPT_API_KEY", "OPENAI_AUTH_TOKEN") or str(
        options.get("apiKey") or options.get("api_key") or ""
    ).strip()
    if not api_key:
        raise RuntimeError("未配置 OpenAI API Key；请设置 OPENAI_API_KEY 或提供 gpt.json")

    base_url = (
        env_first("OPENAI_BASE_URL", "GPT_BASE_URL")
        or str(options.get("baseURL") or options.get("base_url") or "https://api.openai.com/v1")
    ).strip().rstrip("/")
    if not base_url:
        base_url = "https://api.openai.com/v1"

    role_model_env = {
        "cad": ("OPENAI_CAD_REASONING_MODEL", "GPT_CAD_REASONING_MODEL"),
        "fusion": ("OPENAI_FINAL_AGENT_MODEL", "GPT_FINAL_AGENT_MODEL"),
    }.get(role, ())
    requested_model = model or env_first(
        *role_model_env,
        "OPENAI_AGENT_MODEL",
        "GPT_AGENT_MODEL",
        "OPENAI_MODEL",
        "GPT_MODEL",
    )
    selected_model = _select_model(models, requested_model)
    model_config = models.get(selected_model) if isinstance(models.get(selected_model), dict) else {}
    variants = model_config.get("variants") if isinstance(model_config.get("variants"), dict) else {}
    requested_effort = env_first("OPENAI_REASONING_EFFORT", "GPT_REASONING_EFFORT")
    requested_variant = env_first("OPENAI_AGENT_VARIANT", "GPT_AGENT_VARIANT")
    reasoning_effort = requested_effort or requested_variant
    if not reasoning_effort and "low" in variants:
        reasoning_effort = "low"
    reasoning_effort = reasoning_effort.strip() or None

    return OpenAICompatibleConfig(
        api_key=api_key,
        base_url=base_url,
        model=selected_model,
        reasoning_effort=reasoning_effort,
        config_path=loaded_path,
    )
