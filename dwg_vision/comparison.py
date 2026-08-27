from __future__ import annotations

"""Visual audit artifacts for a DWG run.

The comparison images deliberately use the same Scene-local SVG that is sent
to SymPointV2 as their base.  This makes every box/line traceable to the exact
AutoCAD-exported geometry and avoids comparing model coordinates against an
unrelated screenshot or a different DWG converter.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any


TYPE_COLORS: dict[str, tuple[int, int, int]] = {
    "wall": (35, 102, 220),
    "window": (0, 170, 210),
    "door": (235, 126, 30),
    "furniture": (30, 160, 85),
}


def _font(size: int = 22) -> Any:
    from PIL import ImageFont

    candidates = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    )
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _clamp(value: float, lower: float, upper: float) -> int:
    return max(0, min(int(round(value)), int(upper)))


def _local_to_pixel(point: Any, scene: dict[str, Any], image_size: tuple[int, int]) -> tuple[int, int]:
    width, height = image_size
    local_width = max(1e-9, float(scene["local_bounds"][2]))
    local_height = max(1e-9, float(scene["local_bounds"][3]))
    return (
        _clamp(float(point[0]) / local_width * width, 0, width - 1),
        _clamp(float(point[1]) / local_height * height, 0, height - 1),
    )


def _world_to_pixel(box: Any, scene: dict[str, Any], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    world = [float(value) for value in scene["world_bounds"]]
    width, height = image_size
    sx = width / max(1e-9, world[2] - world[0])
    sy = height / max(1e-9, world[3] - world[1])
    x1 = _clamp((float(box[0]) - world[0]) * sx, 0, width - 1)
    y1 = _clamp((float(box[1]) - world[1]) * sy, 0, height - 1)
    x2 = _clamp((float(box[2]) - world[0]) * sx, 0, width - 1)
    y2 = _clamp((float(box[3]) - world[1]) * sy, 0, height - 1)
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def _primitive_lookup(scene: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("primitive_id")): item for item in scene.get("primitives", [])}


def _draw_primitive(draw: Any, primitive: dict[str, Any], scene: dict[str, Any], image_size: tuple[int, int], color: tuple[int, int, int, int]) -> None:
    points = [_local_to_pixel(point, scene, image_size) for point in primitive.get("points_local", [])]
    if primitive.get("command") == "circle" or primitive.get("command") == "ellipse":
        box = primitive.get("bbox_local") or [0, 0, 0, 0]
        left, top = _local_to_pixel((box[0], box[1]), scene, image_size)
        right, bottom = _local_to_pixel((box[2], box[3]), scene, image_size)
        draw.ellipse((min(left, right), min(top, bottom), max(left, right), max(top, bottom)), outline=color, width=2)
    elif len(points) >= 2:
        draw.line(points, fill=color, width=2, joint="curve")


def _draw_legend(draw: Any, image_size: tuple[int, int], counts: Counter[str], *, title: str) -> None:
    font = _font(20)
    labels = [title]
    for kind in ("wall", "window", "door", "furniture"):
        labels.append(f"{kind}: {counts.get(kind, 0)}")
    line_height = 27
    box_width = min(340, max(240, max(len(label) for label in labels) * 12 + 70))
    box_height = 14 + line_height * len(labels)
    x0, y0 = 16, 16
    x1 = min(image_size[0] - 1, x0 + box_width)
    y1 = min(image_size[1] - 1, y0 + box_height)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=(255, 255, 255, 225), outline=(70, 70, 70, 230), width=2)
    draw.text((x0 + 12, y0 + 7), labels[0], fill=(25, 25, 25, 255), font=font)
    for index, kind in enumerate(("wall", "window", "door", "furniture"), start=1):
        y = y0 + 7 + line_height * index
        color = TYPE_COLORS[kind]
        draw.rectangle((x0 + 12, y + 5, x0 + 29, y + 22), fill=(*color, 220))
        draw.text((x0 + 39, y), labels[index], fill=(25, 25, 25, 255), font=font)


def _side_by_side(original: Any, overlay: Any, output_path: Path, *, max_width: int = 12000) -> None:
    from PIL import Image, ImageDraw

    panel_width = max(1, min(original.width, max_width // 2))
    panel_height = max(1, round(original.height * panel_width / max(1, original.width)))
    left = original.convert("RGB").resize((panel_width, panel_height), Image.Resampling.LANCZOS)
    right = overlay.convert("RGB").resize((panel_width, panel_height), Image.Resampling.LANCZOS)
    header_height = 38
    canvas = Image.new("RGB", (panel_width * 2, panel_height + header_height), "white")
    canvas.paste(left, (0, header_height))
    canvas.paste(right, (panel_width, header_height))
    draw = ImageDraw.Draw(canvas)
    font = _font(22)
    draw.text((12, 8), "AutoCAD Scene 原图（清理尺寸标注）", fill=(20, 20, 20), font=font)
    draw.text((panel_width + 12, 8), "识别结果叠加", fill=(20, 20, 20), font=font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)


def generate_scene_comparison(
    scene: dict[str, Any],
    graph: dict[str, Any],
    scene_image_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Create original, detection-overlay, text-evidence, and side-by-side PNGs."""
    from PIL import Image, ImageDraw

    if not scene_image_path or not scene_image_path.is_file():
        raise FileNotFoundError(f"Scene 渲染图不存在：{scene_image_path}")
    original = Image.open(scene_image_path).convert("RGB")
    overlay = original.convert("RGBA")
    draw = ImageDraw.Draw(overlay, "RGBA")
    image_size = original.size
    primitives = _primitive_lookup(scene)
    objects = list(graph.get("objects") or [])
    counts = Counter(str(item.get("type") or "unknown") for item in objects)
    subtype_counts = Counter(str(item.get("subtype") or "unknown") for item in objects)
    review_count = sum(1 for item in objects if item.get("status") == "review")
    label_font = _font(max(14, min(24, image_size[0] // 500)))

    for item in objects:
        kind = str(item.get("type") or "unknown")
        rgb = TYPE_COLORS.get(kind, (150, 70, 180))
        color = (*rgb, 190)
        bbox = ((item.get("geometry_world") or {}).get("bbox") or [0, 0, 0, 0])
        mode = str(item.get("geometry_mode") or "area")
        if mode == "linear" or abs(float(bbox[2]) - float(bbox[0])) < 1e-7 or abs(float(bbox[3]) - float(bbox[1])) < 1e-7:
            for primitive_id in item.get("primitive_ids") or []:
                primitive = primitives.get(str(primitive_id))
                if primitive:
                    _draw_primitive(draw, primitive, scene, image_size, color)
        else:
            left, top, right, bottom = _world_to_pixel(bbox, scene, image_size)
            draw.rounded_rectangle((left, top, right, bottom), radius=4, outline=color, width=3)
            polygon = (item.get("geometry_world") or {}).get("polygon") or []
            if len(polygon) >= 3:
                draw.line([_world_to_pixel([point[0], point[1], point[0], point[1]], scene, image_size)[:2] for point in polygon] + [_world_to_pixel([polygon[0][0], polygon[0][1], polygon[0][0], polygon[0][1]], scene, image_size)[:2]], fill=color, width=3, joint="curve")
            if kind != "wall" or right - left > 30:
                label = f"{kind}/{item.get('subtype') or 'unknown'} {item.get('confidence', 0):.2f}"
                draw.text((left + 3, max(0, top - 24)), label, fill=(*rgb, 255), font=label_font, stroke_width=1, stroke_fill=(255, 255, 255, 220))

    _draw_legend(draw, image_size, counts, title=f"Scene {scene.get('scene_id')} · {len(objects)} objects")

    text_overlay = original.convert("RGBA")
    text_draw = ImageDraw.Draw(text_overlay, "RGBA")
    text_count = Counter(str(item.get("role") or "general_annotation") for item in scene.get("texts", []))
    for text in scene.get("texts", []):
        if text.get("role") == "dimension":
            continue
        box = text.get("bbox_world") or [0, 0, 0, 0]
        left, top, right, bottom = _world_to_pixel(box, scene, image_size)
        text_draw.rectangle((left, top, right, bottom), outline=(232, 170, 0, 220), width=2)
    text_draw.text((16, max(0, image_size[1] - 34)), f"原生文字证据：{len(scene.get('texts', []))} 条（DIMENSION 未绘入视觉底图）", fill=(180, 100, 0, 255), font=_font(20), stroke_width=1, stroke_fill=(255, 255, 255, 220))

    output_dir.mkdir(parents=True, exist_ok=True)
    original_path = output_dir / "original_scene.png"
    overlay_path = output_dir / "detection_overlay.png"
    text_path = output_dir / "native_text_overlay.png"
    side_path = output_dir / "side_by_side.png"
    original.save(original_path)
    overlay.convert("RGB").save(overlay_path)
    text_overlay.convert("RGB").save(text_path)
    _side_by_side(original, overlay, side_path)

    summary = {
        "status": "completed",
        "scene_id": scene.get("scene_id"),
        "source_scene_image": str(scene_image_path),
        "primitive_count": len(scene.get("primitives") or []),
        "native_text_count": len(scene.get("texts") or []),
        "native_text_roles": dict(text_count),
        "object_count": len(objects),
        "review_count": review_count,
        "confirmed_count": len(objects) - review_count,
        "object_counts": dict(counts),
        "subtype_counts": dict(subtype_counts),
        "validation": graph.get("validation") or {},
        "files": {
            "original_scene": str(original_path),
            "detection_overlay": str(overlay_path),
            "native_text_overlay": str(text_path),
            "side_by_side": str(side_path),
        },
        "coordinate_mapping": {
            "space": "world",
            "world_bounds": scene.get("world_bounds"),
            "local_bounds": scene.get("local_bounds"),
            "image_size_px": list(image_size),
        },
        "notes": [
            "底图来自同一 Scene 的 AutoCAD 原生几何，不是重新猜测的图片坐标。",
            "DIMENSION 的数值保留在 scene.json/dwg_raw.json 的原生文字证据中，但默认不绘入视觉底图，避免尺寸文字遮挡家具和墙体。",
        ],
    }
    (output_dir / "comparison_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def write_run_report(
    output_path: Path,
    result: dict[str, Any],
    raw: dict[str, Any],
    comparisons: dict[str, dict[str, Any]],
) -> Path:
    """Write a concise, human-auditable report beside detection.json."""
    report = {
        "schema_version": "run_report.v1",
        "job_id": result.get("job_id"),
        "source": result.get("source"),
        "architecture": [
            "AutoCAD native extraction",
            "frame/Scene split",
            "one SymPointV2 inference per Scene",
            "optional one Scene visual overview plus bounded ROI review",
            "native text-priority Scene Graph fusion",
            "rule validation and visual audit artifacts",
        ],
        "native_extraction": {
            "adapter": raw.get("adapter"),
            "cad_provenance": raw.get("cad_provenance") or {},
            "raw_entity_count": len(raw.get("raw_entities") or raw.get("entities") or []),
            "classified_entity_count": len(raw.get("entities") or []),
            "annotation_count": len(raw.get("annotations") or raw.get("texts") or []),
            "frame_candidate_count": len(raw.get("frames") or raw.get("frame_candidates") or []),
            "world_bounds": (raw.get("coordinate_system") or {}).get("world_bounds") or raw.get("world_bounds"),
        },
        "sympoint": result.get("remote_sympoint"),
        "visual_review": result.get("visual_review"),
        "scenes": comparisons,
        "validation": result.get("validation"),
        "artifacts": result.get("artifacts"),
        "interpretation": {
            "offline_warning": "offline-contract-mode 只验证接口、坐标和可视化链路，不代表 SymPointV2 模型精度。" if result.get("remote_sympoint", {}).get("status") == "offline_contract_mode" else None,
            "text_priority": "原生 TEXT/MTEXT/DIM 作为高权重证据；发生文本与符号冲突时保留冲突并标记 review。",
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path
