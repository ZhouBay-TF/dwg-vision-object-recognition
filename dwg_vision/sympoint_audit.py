from __future__ import annotations

"""Input/output audit rules for SymPointV2.

SymPointV2 is strong on its FloorPlanCAD distribution, but its output is not
an accuracy guarantee for an arbitrary DWG exporter.  This module records the
distribution gaps that are visible before inference and the coverage gaps
that are visible after inference.  It deliberately does not turn a warning
into a fabricated detection or silently repair model coordinates.
"""

from collections import Counter
from typing import Any, Iterable


VAL_REFERENCE = {
    "source": "FloorPlanCAD / SymPointV2 val (810 scenes)",
    "viewbox": "139-140 x 139-140 in the inspected val files",
    "primitive_count_quantiles": {
        "p10": 131,
        "p50": 571,
        "p75": 1256,
        "p90": 2211,
        "p99": 7727,
        "max": 53919,
    },
    "layer_count_quantiles": {
        "min": 2,
        "p10": 8,
        "p50": 15,
        "p90": 30,
        "max": 67,
    },
    "command_mix": {
        "line": 0.8439,
        "arc": 0.1412,
        "circle": 0.0126,
        "ellipse": 0.0011,
    },
    "all_line_scene_fraction": 0.0803,
    "notes": [
        "The public dataset is cropped into 10m x 10m square blocks and only 30% of blocks are retained.",
        "The official loader pads scenes below 2048 primitives; it does not discard scenes above 2048.",
        "The model loader uses normalized coordinates, type features, normalized lengths, widths and layer IDs; RGB is not used by the inspected loader.",
    ],
}


_AUXILIARY_LAYER_MARKERS = (
    "DEFPOINTS",
    "AD-AXIS",
    "DIM_SYMB",
    "DIMENSION",
    "PLOT",
    "PRINT",
    "SHEET",
    "TITLE",
)

_THING_CLASS_NAMES = {
    "single door",
    "double door",
    "sliding door",
    "folding door",
    "revolving door",
    "rolling door",
    "window",
    "bay window",
    "blind window",
    "opening symbol",
    "sofa",
    "bed",
    "chair",
    "table",
    "tv cabinet",
    "wardrobe",
    "cabinet",
    "gas stove",
    "sink",
    "refrigerator",
    "airconditioner",
    "bath",
    "bath tub",
    "washing machine",
    "squat toilet",
    "urinal",
    "toilet",
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pairs(args: Iterable[Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for arg in args:
        if not isinstance(arg, (list, tuple)):
            continue
        values = list(arg)
        for index in range(0, len(values) - 1, 2):
            points.append((_float(values[index]), _float(values[index + 1])))
    return points


def _warning(code: str, message: str, *, severity: str = "warning", **details: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, **details}


def _layer_is_auxiliary(layer: Any) -> bool:
    value = str(layer or "").strip().upper()
    return bool(value) and any(marker in value for marker in _AUXILIARY_LAYER_MARKERS)


def audit_sympoint_input(scene: dict[str, Any], s2: dict[str, Any]) -> dict[str, Any]:
    """Audit one exact Scene SVG->JSON payload before sending it to SPv2."""

    primitives = list(scene.get("primitives") or [])
    commands = list(s2.get("commands") or [])
    args = list(s2.get("args") or [])
    lengths = list(s2.get("lengths") or [])
    layer_ids = list(s2.get("layerIds") or [])
    widths = list(s2.get("widths") or [])
    primitive_count = len(commands)
    scene_count = len(primitives)
    canvas_width = _float(s2.get("width"))
    canvas_height = _float(s2.get("height"))
    points = _pairs(args)
    warnings: list[dict[str, Any]] = []

    if primitive_count != scene_count:
        warnings.append(_warning(
            "primitive_count_mismatch",
            "scene.json 与 scene_s2.json 的图元数量不一致，禁止解释模型索引。",
            severity="error",
            scene_count=scene_count,
            s2_count=primitive_count,
        ))
    if not points or canvas_width <= 0 or canvas_height <= 0:
        warnings.append(_warning(
            "invalid_geometry_payload",
            "SymPointV2 输入缺少有效坐标或画布尺寸。",
            severity="error",
        ))

    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    x_min, x_max = (min(x_values), max(x_values)) if x_values else (0.0, 0.0)
    y_min, y_max = (min(y_values), max(y_values)) if y_values else (0.0, 0.0)
    geometry_width = max(0.0, x_max - x_min)
    geometry_height = max(0.0, y_max - y_min)
    occupancy_width = geometry_width / canvas_width if canvas_width > 0 else 0.0
    occupancy_height = geometry_height / canvas_height if canvas_height > 0 else 0.0

    command_counts = Counter(int(value) for value in commands if str(value).lstrip("-").isdigit())
    nonline_count = sum(command_counts.get(index, 0) for index in (1, 2, 3))
    nonline_ratio = nonline_count / primitive_count if primitive_count else 0.0
    if primitive_count >= 100 and min(occupancy_width, occupancy_height) < 0.60:
        warnings.append(_warning(
            "scene_occupancy_low",
            "有效几何只占 Scene 边界的一小部分，和 FloorPlanCAD 的方形裁剪分布有明显差异；模型坐标会变得稀疏。",
            occupancy_width=round(occupancy_width, 6),
            occupancy_height=round(occupancy_height, 6),
        ))
    if primitive_count >= 100 and nonline_count == 0:
        warnings.append(_warning(
            "all_line_primitives",
            "当前 Scene 的全部图元都被导出为直线；val 中只有约 8.03% 的 Scene 是纯直线。",
            command_counts=dict(command_counts),
        ))

    source_entity_types = Counter(str(item.get("entity_type") or item.get("subtype") or "unknown") for item in primitives)
    block_proxy_count = sum(1 for item in primitives if str(item.get("entity_type") or "") == "AcDbBlockReference")
    block_proxy_ratio = block_proxy_count / scene_count if scene_count else 0.0
    if block_proxy_ratio > 0.02:
        warnings.append(_warning(
            "block_reference_proxy",
            "Scene 中存在较多由 BlockReference 外接框替代的图元；这不是 FloorPlanCAD 的原始 path/circle/ellipse 表达，符号形状可能已丢失。",
            block_proxy_count=block_proxy_count,
            block_proxy_ratio=round(block_proxy_ratio, 6),
        ))

    auxiliary_layers = Counter(
        str(item.get("layer") or "")
        for item in primitives
        if _layer_is_auxiliary(item.get("layer"))
    )
    if auxiliary_layers:
        warnings.append(_warning(
            "auxiliary_layers_present",
            "检测到可能的轴网、尺寸、打印范围或图纸辅助图层；这些图元应在输入规范化阶段显式处理。",
            layers=dict(auxiliary_layers),
        ))

    scene_bounds = scene.get("local_bounds") or [0.0, 0.0, canvas_width, canvas_height]
    scene_width = _float(scene_bounds[2] if len(scene_bounds) > 2 else canvas_width)
    scene_height = _float(scene_bounds[3] if len(scene_bounds) > 3 else canvas_height)
    large_span_count = 0
    for primitive in primitives:
        bbox = primitive.get("bbox_local") or []
        if len(bbox) != 4 or scene_width <= 0 or scene_height <= 0:
            continue
        bbox_width = max(0.0, _float(bbox[2]) - _float(bbox[0]))
        bbox_height = max(0.0, _float(bbox[3]) - _float(bbox[1]))
        if bbox_width >= scene_width * 0.45 and bbox_height >= scene_height * 0.45:
            large_span_count += 1
    if large_span_count:
        warnings.append(_warning(
            "large_span_primitives",
            "存在覆盖大范围的单个图元，可能是打印边界、轴网块或辅助框；应核对其是否进入 SymPointV2。",
            count=large_span_count,
        ))

    max_dimension = max(canvas_width, canvas_height)
    length_clipped_count = sum(1 for value in lengths if _float(value) > max_dimension)
    if primitive_count and length_clipped_count / primitive_count > 0.01:
        warnings.append(_warning(
            "length_feature_clipping",
            "超过 SymPointV2 归一化上限的长度比例较高，模型实际看到的长度特征会被裁成 1。",
            count=length_clipped_count,
            ratio=round(length_clipped_count / primitive_count, 6),
        ))

    if len(set(layer_ids)) <= 1 and primitive_count >= 100:
        warnings.append(_warning(
            "single_layer_input",
            "输入只包含一个图层 ID，LFE 无法利用 CAD 图层上下文。",
            layer_count=len(set(layer_ids)),
        ))

    return {
        "status": "error" if any(item["severity"] == "error" for item in warnings) else ("review_required" if warnings else "pass"),
        "reference": VAL_REFERENCE,
        "scene_id": str(scene.get("scene_id") or ""),
        "primitive_count": primitive_count,
        "padded_count": max(primitive_count, 2048),
        "canvas": {"width": canvas_width, "height": canvas_height},
        "geometry_bbox": [x_min, y_min, x_max, y_max],
        "geometry_occupancy": {
            "width_ratio": round(occupancy_width, 6),
            "height_ratio": round(occupancy_height, 6),
            "area_ratio": round(occupancy_width * occupancy_height, 6),
        },
        "layer_count": len(set(layer_ids)),
        "command_counts": dict(sorted(command_counts.items())),
        "nonline_ratio": round(nonline_ratio, 6),
        "source_entity_types": dict(source_entity_types),
        "block_proxy_count": block_proxy_count,
        "block_proxy_ratio": round(block_proxy_ratio, 6),
        "auxiliary_layers": dict(auxiliary_layers),
        "length_clipped_count": length_clipped_count,
        "warnings": warnings,
        "policy": "allow inference, but keep review_required and do not claim in-distribution accuracy",
    }


def audit_sympoint_output(scene: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Audit model coverage without converting missing detections into objects."""

    primitives = list(scene.get("primitives") or [])
    instances = list(result.get("instances") or [])
    expected = {
        str(item.get("type") or "").strip().lower()
        for item in primitives
        if str(item.get("type") or "").strip().lower() in _THING_CLASS_NAMES
    }
    detected = {
        str(item.get("class_name") or "").strip().lower()
        for item in instances
        if str(item.get("class_name") or "").strip().lower() in _THING_CLASS_NAMES
    }
    warnings: list[dict[str, Any]] = []
    reported_count = result.get("input", {}).get("primitive_count")
    if reported_count is not None and int(reported_count) != len(primitives):
        warnings.append(_warning(
            "output_primitive_count_mismatch",
            "模型结果中的 primitive_count 与当前 Scene 不一致，不能安全映射 primitive_indices。",
            severity="error",
            result_count=reported_count,
            scene_count=len(primitives),
        ))
    if not instances:
        warnings.append(_warning("no_instances", "SymPointV2 没有返回实例候选；保留原始结果，不生成对象。"))
    missing_expected = sorted(expected - detected)
    if missing_expected:
        warnings.append(_warning(
            "expected_thing_not_detected",
            "原生几何分类中存在可计数图元，但 SymPointV2 没有返回对应 thing 实例；必须交给视觉/文本证据复核。",
            expected=sorted(expected),
            missing=missing_expected,
        ))

    semantic_scores = [
        _float(item.get("score"))
        for item in list(result.get("semantic_by_primitive") or [])
        if item.get("score") is not None
    ]
    instance_scores = [_float(item.get("score")) for item in instances if item.get("score") is not None]
    class_counts = Counter(str(item.get("class_name") or "unknown") for item in instances)
    return {
        "status": "error" if any(item["severity"] == "error" for item in warnings) else ("review_required" if warnings else "pass"),
        "scene_id": str(scene.get("scene_id") or result.get("scene_id") or ""),
        "instance_count": len(instances),
        "instance_class_counts": dict(class_counts),
        "thing_classes_expected_from_native": sorted(expected),
        "thing_classes_detected": sorted(detected),
        "missing_expected_thing_classes": missing_expected,
        "semantic_score_summary": {
            "count": len(semantic_scores),
            "max": max(semantic_scores) if semantic_scores else None,
            "median": sorted(semantic_scores)[len(semantic_scores) // 2] if semantic_scores else None,
        },
        "instance_score_summary": {
            "count": len(instance_scores),
            "min": min(instance_scores) if instance_scores else None,
            "max": max(instance_scores) if instance_scores else None,
        },
        "warnings": warnings,
        "interpretation": "semantic_by_primitive score is not an object confidence; object confidence comes from instance.score, and missing thing instances remain unresolved.",
    }
