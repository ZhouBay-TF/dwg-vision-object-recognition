from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any


BATHROOM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "bathroom_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "is_bathroom": {"type": "boolean"},
                    "room_label": {"type": "string"},
                    "bbox_px": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "is_bathroom",
                    "room_label",
                    "bbox_px",
                    "confidence",
                    "evidence",
                ],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["bathroom_candidates", "notes"],
}


def _image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(suffix, "image/png")
    return f"data:{mime};base64,{encoded}"


def _prompt(tile_id: str, tile: dict[str, Any]) -> str:
    return f"""你是建筑 CAD 平面图识别助手。现在分析楼层平面图的局部图像。

当前分块：{tile_id}
图像画布尺寸：{tile['canvas_width']} x {tile['canvas_height']} 像素
有效绘图区：左上角 ({tile['x']}, {tile['y']})，大小 {tile['width']} x {tile['height']} 像素。

任务：只识别“独立的卫生间/厕所/洗手间/卫浴/公厕”等房间或空间，并返回它们在当前分块中的像素框。

规则：
1. 不读取、不依赖图层名、块名或图层颜色，只根据可见几何、文字、洁具符号和空间关系判断。
2. 只统计房间/空间，不统计马桶、洗手盆、淋浴器等单个洁具。
3. 同一个卫生间即使看到了多个洁具，也只能返回一个候选框。
4. 只返回在图中确实可见的候选；看不清时返回空数组，不要猜测。
    5. bbox_px 使用 [left, top, right, bottom]，必须落在当前画布内。
6. confidence 为 0 到 1 之间的判断置信度，evidence 简要说明可见依据。
"""


def analyze_tile(
    client: Any,
    tile_path: Path,
    tile: dict[str, Any],
    *,
    model: str,
    image_detail: str,
) -> dict[str, Any]:
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _prompt(tile["tile_id"], tile)},
                    {
                        "type": "input_image",
                        "image_url": _image_data_url(tile_path),
                        "detail": image_detail,
                    },
                ],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "bathroom_detection",
                "strict": True,
                "schema": BATHROOM_SCHEMA,
            }
        },
    )
    output_text = response.output_text
    if not output_text:
        raise RuntimeError(f"分块 {tile['tile_id']} 没有返回可解析结果")
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"分块 {tile['tile_id']} 返回的不是 JSON：{output_text}") from exc
    parsed["tile_id"] = tile["tile_id"]
    return parsed


def _global_bbox(local_bbox: list[int], tile: dict[str, Any]) -> list[int]:
    left, top, right, bottom = [int(value) for value in local_bbox]
    left = max(0, min(tile["canvas_width"], left))
    top = max(0, min(tile["canvas_height"], top))
    right = max(0, min(tile["canvas_width"], right))
    bottom = max(0, min(tile["canvas_height"], bottom))
    return [left + tile["x"], top + tile["y"], right + tile["x"], bottom + tile["y"]]


def _area(box: list[int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _iou(first: list[int], second: list[int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = _area([left, top, right, bottom])
    union = _area(first) + _area(second) - intersection
    return intersection / union if union else 0.0


def _same_room(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_box = first["bbox_global_px"]
    second_box = second["bbox_global_px"]
    if _iou(first_box, second_box) >= 0.20:
        return True
    # Overlapping edge tiles can lead to one box being much smaller than the other.
    def center(box: list[int]) -> tuple[float, float]:
        return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

    first_center = center(first_box)
    second_center = center(second_box)
    distance = ((first_center[0] - second_center[0]) ** 2 + (first_center[1] - second_center[1]) ** 2) ** 0.5
    scale = max(1.0, min(first_box[2] - first_box[0], first_box[3] - first_box[1]))
    return distance <= scale * 0.35


def _deduplicate(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: item["confidence"], reverse=True):
        duplicate = next((item for item in unique if _same_room(candidate, item)), None)
        if duplicate is None:
            unique.append(candidate)
        elif candidate["confidence"] > duplicate["confidence"]:
            unique[unique.index(duplicate)] = candidate
    return unique


def analyze_manifest(
    manifest_path: Path,
    *,
    model: str | None = None,
    image_detail: str | None = None,
    min_confidence: float = 0.60,
    limit: int | None = None,
) -> dict[str, Any]:
    """Analyze every tile and return deduplicated bathroom candidates."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency message
        raise RuntimeError("视觉识别需要安装 openai") from exc

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    client = OpenAI()
    selected_model = model or os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
    selected_detail = image_detail or os.getenv("OPENAI_IMAGE_DETAIL", "high")
    root = manifest_path.parent
    raw_candidates: list[dict[str, Any]] = []
    processed = 0

    for page in manifest["pages"]:
        for tile in page["tiles"]:
            if limit is not None and processed >= limit:
                break
            tile_path = root / tile["path"]
            result = analyze_tile(
                client,
                tile_path,
                tile,
                model=selected_model,
                image_detail=selected_detail,
            )
            processed += 1
            for candidate in result.get("bathroom_candidates", []):
                if not candidate.get("is_bathroom", False):
                    continue
                bbox = candidate.get("bbox_px", [])
                if len(bbox) != 4 or _area([int(value) for value in bbox]) == 0:
                    continue
                raw_candidates.append(
                    {
                        "tile_id": tile["tile_id"],
                        "room_label": candidate.get("room_label", ""),
                        "bbox_local_px": [int(value) for value in bbox],
                        "bbox_global_px": _global_bbox(bbox, tile),
                        "confidence": max(0.0, min(1.0, float(candidate.get("confidence", 0)))),
                        "evidence": candidate.get("evidence", ""),
                    }
                )
        if limit is not None and processed >= limit:
            break

    unique = _deduplicate(raw_candidates)
    confirmed = [item for item in unique if item["confidence"] >= min_confidence]
    review = [item for item in unique if item["confidence"] < min_confidence]
    return {
        "schema_version": "0.1",
        "source": manifest.get("source"),
        "model": selected_model,
        "image_detail": selected_detail,
        "processed_tiles": processed,
        "bathroom_count": len(confirmed),
        "review_count": len(review),
        "all_deduplicated_candidates": unique,
    }
