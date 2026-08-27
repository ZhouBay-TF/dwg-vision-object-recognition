from __future__ import annotations

"""Vision provider adapters and a deterministic offline provider for tests."""

import base64
import json
import re
from pathlib import Path
from typing import Any, Callable, Protocol

from .env_config import env_first


DETECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "detections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "panel_id": {"type": "string"},
                    "type": {"type": "string", "enum": ["wall", "window", "door", "furniture"]},
                    "subtype": {"type": "string"},
                    "bbox_px": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "polygon_px": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                    "rotation_deg": {"type": "number"},
                    "dimensions": {"type": "object"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string"},
                    "nearby_text": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["panel_id", "type", "subtype", "bbox_px", "rotation_deg", "dimensions", "confidence", "evidence", "nearby_text"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["detections", "notes"],
}


class VisionProvider(Protocol):
    name: str
    model: str

    def analyze(self, image_path: Path, prompt: str) -> dict[str, Any]:
        ...


def _image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(path.suffix.lower(), "image/png")
    return f"data:{mime};base64,{encoded}"


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("视觉模型没有返回 choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or getattr(item, "content", "")))
        return "".join(parts)
    return str(content)


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"视觉模型返回的不是 JSON：{text[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("视觉模型 JSON 顶层必须是对象")
    parsed.setdefault("detections", [])
    parsed.setdefault("notes", "")
    return parsed


def build_detection_prompt(mosaic: dict[str, Any], *, tile_limit: int | None = None) -> str:
    panels = mosaic.get("panels", [])
    panel_lines = "\n".join(
        f"- panel_id={panel['panel_id']}, 左上角=({panel['x']},{panel['y']}), 尺寸={panel['width']}x{panel['height']}，映射到整图左上角=({panel['global_x']},{panel['global_y']})，缩放={panel['scale']:.6f}"
        for panel in panels
    )
    return f"""你是建筑 CAD 平面图视觉核验器。请分析这张由多个同尺度局部图组成的拼图。

拼图画布尺寸：{mosaic['width']} x {mosaic['height']} 像素。
面板坐标：
{panel_lines}

任务：识别所有可见的墙体、窗户、门、家具。每一个明显独立的对象只返回一次；跨面板对象可在相邻面板分别返回，后续会由程序按坐标去重。

严格要求：
1. 只返回你能在图中看见的对象，不要根据常识补画不存在的对象。
2. panel_id 必须是上面给出的值；bbox_px 是该 panel 内的局部像素框 [left, top, right, bottom]，不能用拼图全局坐标。
3. 墙体优先用可见的墙线/墙带框出；窗户/门用符号所在的最小框；家具用家具轮廓的最小框。
   如果对象轮廓清晰，请额外返回 polygon_px（panel 内局部坐标的闭合近似多边形），它用于生成类似全景分割的矢量结果；看不清时可以为空数组。
4. subtype 使用简短中文或英文，例如：外墙、内墙、推拉窗、平开门、床、沙发、桌、椅、柜、卫浴器具。卫浴器具仍归 furniture，但必须在 subtype 中标明 sanitary_fixture。
5. dimensions 中保留图上能可靠估计的 width_px、height_px、length_px；无法判断的值不要猜，仍可保留空对象。
6. evidence 是一句可复核的可见依据；nearby_text 只填写紧邻对象的可见标注文字。
7. confidence 为 0 到 1。标注文字和几何符号明显冲突时降低 confidence，并在 evidence 说明冲突。
8. 不要输出思维链，只输出简短的可审计依据和不确定性说明。

请只返回 JSON：{{"detections": [...], "notes": "..."}}。"""


class _OpenAICompatibleVision:
    name = "openai-compatible"

    def __init__(self, *, api_key: str | None, base_url: str, model: str, name: str) -> None:
        if not api_key:
            raise RuntimeError(f"{name} 需要 API key 环境变量")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("模型调用需要安装 openai") from exc
        self.name = name
        self.model = model
        try:
            timeout_s = float(env_first("VISION_REQUEST_TIMEOUT_S", default="120"))
        except ValueError:
            timeout_s = 120.0
        self.request_timeout_s = max(1.0, timeout_s)
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self.request_timeout_s,
            max_retries=0,
        )

    def analyze(self, image_path: Path, prompt: str) -> dict[str, Any]:
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "你是严谨的建筑平面图视觉识别器。你的输出必须是 JSON 对象。",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
                    ],
                },
            ],
        )
        return _parse_json(_response_text(response))


class DeepSeekVisionProvider(_OpenAICompatibleVision):
    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        super().__init__(
            api_key=api_key or env_first("DEEPSEEK_API_KEY", "deepseek"),
            base_url=env_first("DEEPSEEK_BASE_URL", default="https://api.deepseek.com"),
            model=model or env_first("DEEPSEEK_VISION_MODEL", "DEEPSEEK_MODEL", default="deepseek-v4-flash-vision-exp"),
            name="deepseek",
        )


class ArkVisionProvider(_OpenAICompatibleVision):
    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        # ANTHROPIC_* is the compatibility naming used by some OpenCode/Ark
        # configurations. ARK_* remains the documented project contract.
        selected_model = model or env_first("ARK_VISION_MODEL", "ANTHROPIC_MODEL")
        if not selected_model:
            raise RuntimeError("ARK_VISION_MODEL/ANTHROPIC_MODEL 未配置；请填入 Ark 上可用的视觉理解模型")
        if "seedream" in selected_model.lower():
            raise RuntimeError(
                "ARK_VISION_MODEL 当前配置为 Seedream 图片生成模型；它不能可靠返回检测 JSON。"
                "请将 ARK_VISION_MODEL 设置为 Ark 上可用的视觉理解模型，Seedream 仅作为可选增强/编辑模型。"
            )
        super().__init__(
            api_key=api_key or env_first("ARK_API_KEY", "ANTHROPIC_AUTH_TOKEN", "huoshanfnagzhou"),
            base_url=env_first(
                "ARK_BASE_URL",
                "ANTHROPIC_BASE_URL",
                default="https://ark.cn-beijing.volces.com/api/v3",
            ),
            model=selected_model,
            name="ark",
        )


class CallableVisionProvider:
    """Small adapter useful for unit tests and local model servers."""

    def __init__(self, name: str, model: str, callback: Callable[[Path, str], dict[str, Any]]) -> None:
        self.name = name
        self.model = model
        self._callback = callback

    def analyze(self, image_path: Path, prompt: str) -> dict[str, Any]:
        return self._callback(image_path, prompt)


class OfflineVisionProvider(CallableVisionProvider):
    def __init__(self) -> None:
        super().__init__("offline", "offline-heuristic", lambda _path, _prompt: {"detections": [], "notes": "offline mode: visual model disabled"})
