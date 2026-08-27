"""Edit a local image with Gemini, then detect objects in the edited image.

The script deliberately uses two calls:

1. An image-capable Gemini model edits the image and returns image bytes.
2. A vision model receives those bytes and returns strict JSON with positions.

The API key is read from GEMINI_API_KEY (or GOOGLE_API_KEY); it is never
accepted as a command-line argument, so it is less likely to end up in shell
history or process listings.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any


MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024
# Nano Banana 2. Use gemini-2.5-flash-image for the legacy Nano Banana model.
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_VISION_MODEL = "gemini-flash-latest"


OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "image_width": {"type": "integer"},
        "image_height": {"type": "integer"},
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "主要物体的简短名称。",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "0 到 1 之间的置信度。",
                    },
                    "bbox_px": {
                        "type": "array",
                        "description": "像素框 [left, top, right, bottom]，原点在左上角。",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "center_px": {
                        "type": "array",
                        "description": "像素中心点 [x, y]。",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "normalized_bbox": {
                        "type": "array",
                        "description": "归一化框 [left, top, right, bottom]，范围 0 到 1。",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "description": {
                        "type": "string",
                        "description": "物体的简短可见特征。",
                    },
                },
                "required": [
                    "name",
                    "confidence",
                    "bbox_px",
                    "center_px",
                    "normalized_bbox",
                    "description",
                ],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["image_width", "image_height", "objects", "notes"],
}


def _load_dotenv() -> None:
    """Load .env when python-dotenv is installed, without requiring it."""

    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _mime_type(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0]
    if mime in {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/gif",
        "image/avif",
        "image/bmp",
        "image/tiff",
    }:
        return mime
    raise ValueError(
        f"不支持的图片格式: {path.suffix or '<无扩展名>'}。"
        "请使用 PNG、JPEG、WEBP、HEIC、HEIF、GIF 或 AVIF。"
    )


def _image_dimensions(image_bytes: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as image:
            return image.size
    except Exception:
        return None, None


def _api_url(api_version: str, model: str) -> str:
    if not model or "/" in model or ":" in model:
        raise ValueError(f"非法模型名: {model!r}")
    return (
        f"https://generativelanguage.googleapis.com/{api_version}/models/"
        f"{model}:generateContent"
    )


def _post_json(
    *,
    api_key: str,
    api_version: str,
    model: str,
    payload: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        _api_url(api_version, model),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini API 请求失败（HTTP {exc.code}，模型 {model}）：{detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 Gemini API：{exc.reason}") from exc

    try:
        parsed = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini API 返回的不是 JSON。") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini API 返回的 JSON 顶层不是对象。")
    return parsed


def _response_parts(response: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for candidate in response.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            if isinstance(part, dict):
                parts.append(part)
    return parts


def _extract_image(response: dict[str, Any]) -> tuple[bytes, str]:
    # For Gemini image models the final image is the last returned image part.
    image_parts: list[tuple[bytes, str]] = []
    for part in _response_parts(response):
        inline_data = part.get("inlineData") or part.get("inline_data")
        if not isinstance(inline_data, dict):
            continue
        encoded = inline_data.get("data")
        if not encoded:
            continue
        mime_type = inline_data.get("mimeType") or inline_data.get(
            "mime_type", "image/png"
        )
        try:
            image_parts.append((base64.b64decode(encoded), str(mime_type)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Gemini 返回了无法解码的图片数据。") from exc

    if not image_parts:
        message = response.get("promptFeedback") or response.get("error") or response
        raise RuntimeError(f"Gemini 没有返回图片：{json.dumps(message, ensure_ascii=False)}")
    return image_parts[-1]


def _extract_text(response: dict[str, Any]) -> str:
    return "\n".join(
        str(part["text"])
        for part in _response_parts(response)
        if isinstance(part.get("text"), str)
    ).strip()


def _parse_json_text(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].lstrip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"物体识别返回的不是合法 JSON：{text}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("物体识别返回的 JSON 顶层不是对象。")
    return parsed


def _edit_image(
    *,
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    image_model: str,
    api_key: str,
    api_version: str,
    timeout: int,
    aspect_ratio: str | None,
    image_size: str | None,
) -> tuple[bytes, str, str]:
    generation_config: dict[str, Any] = {
        "responseModalities": ["TEXT", "IMAGE"],
    }
    image_config: dict[str, str] = {}
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    if image_size:
        image_config["imageSize"] = image_size
    if image_config:
        generation_config["responseFormat"] = {"image": image_config}

    response = _post_json(
        api_key=api_key,
        api_version=api_version,
        model=image_model,
        timeout=timeout,
        payload={
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": generation_config,
        },
    )
    edited_bytes, edited_mime = _extract_image(response)
    return edited_bytes, edited_mime, _extract_text(response)


def _detect_objects(
    *,
    image_bytes: bytes,
    mime_type: str,
    vision_model: str,
    api_key: str,
    api_version: str,
    timeout: int,
) -> dict[str, Any]:
    width, height = _image_dimensions(image_bytes)
    dimensions = (
        f"图像宽度为 {width} 像素，高度为 {height} 像素。"
        if width is not None and height is not None
        else "请先读取图像实际宽高。"
    )
    prompt = f"""请分析这张已经完成样式修改的图片，只返回图中主要且清晰可见的物体。
{dimensions}

对每个物体返回：
- name：简短名称；
- confidence：0 到 1 的置信度；
- bbox_px：[left, top, right, bottom]，以像素为单位，原点在左上角；
- center_px：[x, y]，以像素为单位；
- normalized_bbox：[left, top, right, bottom]，分别除以图像宽度和高度，范围 0 到 1；
- description：一句话描述可见依据。

不要把整张背景、阴影或无法明确辨认的区域当成物体。若没有可靠的主要物体，objects 返回空数组。"""

    response = _post_json(
        api_key=api_key,
        api_version=api_version,
        model=vision_model,
        timeout=timeout,
        payload={
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": OBJECT_SCHEMA,
            },
        },
    )
    text = _extract_text(response)
    if not text:
        raise RuntimeError("物体识别请求没有返回文本 JSON。")
    parsed = _parse_json_text(text)
    if width is not None:
        parsed["image_width"] = width
    if height is not None:
        parsed["image_height"] = height
    return parsed


def _default_output_image(output_dir: Path, mime_type: str) -> Path:
    extension = {
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/png": ".png",
    }.get(mime_type, ".png")
    return output_dir / f"edited{extension}"


def run(args: argparse.Namespace) -> dict[str, Any]:
    _load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未找到 API Key。请先设置 GEMINI_API_KEY 环境变量，或在 .env 中设置它。"
        )

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到输入图片: {input_path}")
    mime_type = _mime_type(input_path)
    image_bytes = input_path.read_bytes()
    if len(image_bytes) > MAX_INLINE_IMAGE_BYTES:
        raise ValueError(
            f"输入图片为 {len(image_bytes) / 1024 / 1024:.1f} MB，"
            "超过 inline_data 的 20 MB 限制；请先压缩图片或改用 Files API。"
        )

    edited_bytes, edited_mime, edit_text = _edit_image(
        image_bytes=image_bytes,
        mime_type=mime_type,
        prompt=args.prompt,
        image_model=args.image_model,
        api_key=api_key,
        api_version=args.api_version,
        timeout=args.timeout,
        aspect_ratio=args.aspect_ratio,
        image_size=args.image_size,
    )

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_image = (
        args.output_image.expanduser().resolve()
        if args.output_image
        else _default_output_image(output_dir, edited_mime)
    )
    output_image.parent.mkdir(parents=True, exist_ok=True)
    output_image.write_bytes(edited_bytes)

    objects = _detect_objects(
        image_bytes=edited_bytes,
        mime_type=edited_mime,
        vision_model=args.vision_model,
        api_key=api_key,
        api_version=args.api_version,
        timeout=args.timeout,
    )

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "input_image": str(input_path),
        "edit_prompt": args.prompt,
        "image_model": args.image_model,
        "vision_model": args.vision_model,
        "edited_image": {
            "path": str(output_image),
            "mime_type": edited_mime,
            "width": objects.get("image_width"),
            "height": objects.get("image_height"),
        },
        "edit_description": edit_text,
        "objects": objects.get("objects", []),
        "analysis_notes": objects.get("notes", ""),
    }
    if not args.no_image_data:
        result["edited_image"]["data_url"] = (
            f"data:{edited_mime};base64,{base64.b64encode(edited_bytes).decode('ascii')}"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用 Gemini 修改本地图片，并以 JSON 返回编辑图和主要物体位置。"
    )
    parser.add_argument("--input", required=True, type=Path, help="输入图片路径")
    parser.add_argument(
        "--prompt",
        required=True,
        help="样式修改指令，例如：改成水彩插画风格，保持主体位置不变。",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("runs/gemini_edit"), help="输出目录"
    )
    parser.add_argument("--output-image", type=Path, help="编辑图片输出路径")
    parser.add_argument(
        "--output-json", type=Path, help="JSON 输出路径；默认写入 output-dir/result.json"
    )
    parser.add_argument(
        "--image-model",
        default=os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_IMAGE_MODEL),
        help=f"Nano Banana 图片编辑模型（默认 {DEFAULT_IMAGE_MODEL}）",
    )
    parser.add_argument(
        "--vision-model",
        default=os.getenv("GEMINI_VISION_MODEL", DEFAULT_VISION_MODEL),
        help=f"物体识别模型（默认 {DEFAULT_VISION_MODEL}）",
    )
    parser.add_argument(
        "--api-version", default=os.getenv("GEMINI_API_VERSION", "v1beta")
    )
    parser.add_argument("--timeout", type=int, default=180, help="每次请求的超时秒数")
    parser.add_argument("--aspect-ratio", help="可选，如 1:1、16:9、3:2")
    parser.add_argument(
        "--image-size", choices=["0.5K", "1K", "2K", "4K"], help="可选图片输出尺寸"
    )
    parser.add_argument(
        "--no-image-data",
        action="store_true",
        help="JSON 中不嵌入 data URL，只返回编辑图片路径",
    )
    return parser


def main() -> int:
    _load_dotenv()
    args = build_parser().parse_args()
    try:
        result = run(args)
        output_path = (
            args.output_json.expanduser().resolve()
            if args.output_json
            else args.output_dir.expanduser().resolve() / "result.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(output_path)
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
