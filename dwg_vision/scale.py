from __future__ import annotations

"""Extract native drawing-scale evidence before Scene cropping.

The scale bar is evidence about the drawing-to-paper relationship, not a
SymPointV2 object.  We therefore detect it from AutoCAD TEXT/MTEXT/DIM data,
record the evidence, and remove its local region from the effective geometry
used by the model.  No visual-model guess is used as a scale anchor.
"""

import re
import unicodedata
from typing import Any

from .contracts import require_bbox


_RATIO_RE = re.compile(
    r"(?<![\d.])(?P<numerator>\d+(?:\.\d+)?)\s*"
    r"(?:[:：/／])\s*(?P<denominator>\d+(?:\.\d+)?)(?![\d.])"
)
_SCALE_HINT_RE = re.compile(r"(?:scale|比例尺|比例|缩尺|缩放|\bs\b)", re.IGNORECASE)


def normalize_scale_text(value: Any) -> str:
    """Normalize common CAD/full-width spellings without changing evidence."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\\[A-Za-z][^;]*;", " ", text)
    text = text.replace("﹕", ":").replace("∶", ":")
    return re.sub(r"\s+", " ", text).strip()


def parse_scale_ratios(value: Any) -> list[dict[str, Any]]:
    """Return architectural scale ratios such as ``1:100`` or ``1/50``."""
    text = normalize_scale_text(value)
    result: list[dict[str, Any]] = []
    for match in _RATIO_RE.finditer(text):
        numerator = float(match.group("numerator"))
        denominator = float(match.group("denominator"))
        if numerator <= 0.0 or denominator <= 0.0 or denominator < numerator:
            continue
        result.append({
            "ratio_text": match.group(0),
            "numerator": numerator,
            "denominator": denominator,
            "text_prefix": text[:match.start()],
            "scale_hint": bool(_SCALE_HINT_RE.search(text[:match.start()] or text)),
        })
    return result


def _annotation_bbox(annotation: dict[str, Any]) -> list[float]:
    bbox = annotation.get("bbox_world") or annotation.get("bbox_px")
    if bbox:
        return require_bbox(bbox, "scale annotation bbox")
    position = annotation.get("position_world") or annotation.get("position") or [0.0, 0.0]
    x, y = float(position[0]), float(position[1])
    height = max(float(annotation.get("height") or annotation.get("text_height") or 1.0), 1.0)
    return [x, y, x + height, y + height]


def _intersects(first: list[float], second: list[float]) -> bool:
    return not (
        first[2] < second[0]
        or second[2] < first[0]
        or first[3] < second[1]
        or second[3] < first[1]
    )


def _expand(box: list[float], margin: float) -> list[float]:
    return [box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin]


def _unit_name(raw: dict[str, Any]) -> str:
    coordinate_system = raw.get("coordinate_system") or {}
    return str(coordinate_system.get("units") or "drawing_units")


def _empty_evidence(raw: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "cad_scale.v1",
        "status": "not_found",
        "usable": False,
        "reason": reason,
        "source": "native_annotation",
        "drawing_units": _unit_name(raw),
        "candidate_count": 0,
        "candidates": [],
    }


def build_scale_override_evidence(raw: dict[str, Any], value: Any) -> dict[str, Any]:
    """Build auditable scale evidence when the title block was cropped away.

    A simplified/exported DWG may no longer contain the native ``1:100``
    annotation even though the operator has confirmed it from the source
    drawing.  The override is deliberately explicit and is never inferred by
    a vision model.  It also has no exclusion bbox because there is no native
    scale-label geometry left to remove.
    """
    text = normalize_scale_text(value)
    ratios = parse_scale_ratios(text)
    if len(ratios) != 1:
        raise ValueError("显式比例尺必须恰好包含一个有效比例，例如 1:100")
    ratio = ratios[0]
    candidate = {
        "candidate_id": "scale_user_override",
        "text_id": "user_scale_override",
        "text": text,
        "ratio_text": ratio["ratio_text"],
        "numerator": ratio["numerator"],
        "denominator": ratio["denominator"],
        "bbox_world": None,
        "position_world": None,
        "layer": "",
        "source": "user_override",
        "normalized_position": None,
        "location": "explicit_override",
        "location_score": 1.0,
        "selection_score": 1.0,
        "scale_hint": True,
        "search_scope": "explicit_override",
    }
    return {
        "schema_version": "cad_scale.v1",
        "status": "detected",
        "usable": True,
        "reason": "explicit_user_scale_override",
        "search_scope": "explicit_override",
        "source": "user_override",
        "drawing_units": _unit_name(raw),
        "text_id": "user_scale_override",
        "text_ids": ["user_scale_override"],
        "text": text,
        "layer": "",
        "ratio_text": ratio["ratio_text"],
        "numerator": ratio["numerator"],
        "denominator": ratio["denominator"],
        "world_units_per_paper_unit_same_unit": ratio["denominator"] / ratio["numerator"],
        "paper_units_per_world_unit_same_unit": ratio["numerator"] / ratio["denominator"],
        "bbox_world": None,
        "position_world": None,
        "normalized_position": None,
        "location": "explicit_override",
        "confidence": 1.0,
        "exclusion_bbox_world": None,
        "candidate_count": 1,
        "candidates": [candidate],
        "unit_conversion_note": "比例尺来自显式人工确认；由于标题栏已裁剪，没有原生比例尺区域可排除。比例换算仅在 DWG drawing_units 与纸张单位一致时成立。",
    }


def _collect_candidates(
    annotations: Any,
    *,
    search_bounds: list[float],
    score_bounds: list[float],
    search_scope: str,
) -> list[dict[str, Any]]:
    score_width = max(score_bounds[2] - score_bounds[0], 1e-9)
    score_height = max(score_bounds[3] - score_bounds[1], 1e-9)
    candidates: list[dict[str, Any]] = []
    for index, annotation in enumerate(annotations):
        if not isinstance(annotation, dict):
            continue
        text = normalize_scale_text(annotation.get("text") or annotation.get("text_string"))
        if not text:
            continue
        bbox = _annotation_bbox(annotation)
        if not _intersects(bbox, search_bounds):
            continue
        position = annotation.get("position_world") or annotation.get("position")
        if isinstance(position, (list, tuple)) and len(position) >= 2:
            center_x, center_y = float(position[0]), float(position[1])
        else:
            center_x = (bbox[0] + bbox[2]) / 2.0
            center_y = (bbox[1] + bbox[3]) / 2.0
        x_norm = (center_x - score_bounds[0]) / score_width
        y_norm = (center_y - score_bounds[1]) / score_height
        right_score = max(0.0, min(1.0, x_norm))
        bottom_score = max(0.0, min(1.0, 1.0 - y_norm))
        location_score = 0.5 * right_score + 0.5 * bottom_score
        for ratio in parse_scale_ratios(text):
            is_lower_right = x_norm >= 0.50 and y_norm <= 0.50
            score = location_score + (0.12 if ratio["scale_hint"] else 0.0) + (0.08 if is_lower_right else 0.0)
            candidates.append({
                "candidate_id": f"scale_{index:05d}_{len(candidates):03d}",
                "text_id": str(annotation.get("text_id") or annotation.get("id") or annotation.get("handle") or f"text_{index:05d}"),
                "text": text,
                "ratio_text": ratio["ratio_text"],
                "numerator": ratio["numerator"],
                "denominator": ratio["denominator"],
                "bbox_world": bbox,
                "position_world": [center_x, center_y],
                "layer": str(annotation.get("layer") or ""),
                "source": str(annotation.get("source") or "native_annotation"),
                "normalized_position": {"x": x_norm, "y": y_norm},
                "location": "lower_right" if is_lower_right else "other",
                "location_score": round(location_score, 6),
                "selection_score": round(score, 6),
                "scale_hint": bool(ratio["scale_hint"]),
                "search_scope": search_scope,
            })
    return candidates


def extract_scale_evidence(
    raw: dict[str, Any],
    frame: dict[str, Any],
    *,
    scale_override: Any | None = None,
) -> dict[str, Any]:
    """Find the most credible native scale label for one drawing frame.

    AutoCAD coordinates use increasing Y upwards, so the expected lower-right
    title-block position is high X and low Y.  Candidates outside a small
    frame margin are ignored.  If two different ratios are equally plausible,
    the result is marked ambiguous and downstream calibration must not use it.
    """
    if normalize_scale_text(scale_override):
        return build_scale_override_evidence(raw, scale_override)

    frame_bounds = require_bbox(frame.get("world_bbox"), "frame.world_bbox")
    frame_width = max(frame_bounds[2] - frame_bounds[0], 1e-9)
    frame_height = max(frame_bounds[3] - frame_bounds[1], 1e-9)
    frame_margin = max(frame_width, frame_height) * 0.10
    search_bounds = _expand(frame_bounds, frame_margin)
    annotations = raw.get("annotations") or raw.get("texts") or []
    candidates = _collect_candidates(
        annotations,
        search_bounds=search_bounds,
        score_bounds=frame_bounds,
        search_scope="frame",
    )
    search_scope = "frame"
    scale_scope_bounds = frame_bounds
    if not candidates:
        global_bounds = (raw.get("coordinate_system") or {}).get("world_bounds") or raw.get("world_bounds")
        if global_bounds:
            global_bounds = require_bbox(global_bounds, "drawing world bounds")
            global_margin = max(global_bounds[2] - global_bounds[0], global_bounds[3] - global_bounds[1]) * 0.02
            candidates = _collect_candidates(
                annotations,
                search_bounds=_expand(global_bounds, global_margin),
                score_bounds=global_bounds,
                search_scope="drawing_bounds_fallback",
            )
            search_scope = "drawing_bounds_fallback"
            scale_scope_bounds = global_bounds

    if not candidates:
        return _empty_evidence(raw, reason="no_native_scale_ratio_text_in_frame")

    candidates.sort(key=lambda item: (item["selection_score"], item["location_score"]), reverse=True)
    selected = candidates[0]
    different_ratio = [
        item for item in candidates[1:]
        if abs(float(item["denominator"]) - float(selected["denominator"])) > 1e-9
    ]
    ambiguous = bool(
        different_ratio
        and float(selected["selection_score"]) - float(different_ratio[0]["selection_score"]) < 0.12
    )
    text_height = max(selected["bbox_world"][3] - selected["bbox_world"][1], 1.0)
    scope_width = max(scale_scope_bounds[2] - scale_scope_bounds[0], 1e-9)
    scope_height = max(scale_scope_bounds[3] - scale_scope_bounds[1], 1e-9)
    exclusion_margin = max(max(scope_width, scope_height) * 0.01, text_height * 4.0)
    evidence = {
        "schema_version": "cad_scale.v1",
        "status": "ambiguous" if ambiguous else "detected",
        "usable": not ambiguous,
        "reason": "multiple_competing_scale_ratios" if ambiguous else "native_scale_ratio_selected",
        "search_scope": search_scope,
        "source": "autocad_native_annotation" if selected["source"].startswith("autocad") else selected["source"],
        "drawing_units": _unit_name(raw),
        "text_id": selected["text_id"],
        "text_ids": sorted({str(item["text_id"]) for item in candidates}),
        "text": selected["text"],
        "layer": selected["layer"],
        "ratio_text": selected["ratio_text"],
        "numerator": selected["numerator"],
        "denominator": selected["denominator"],
        "world_units_per_paper_unit_same_unit": selected["denominator"] / selected["numerator"],
        "paper_units_per_world_unit_same_unit": selected["numerator"] / selected["denominator"],
        "bbox_world": selected["bbox_world"],
        "position_world": selected["position_world"],
        "normalized_position": selected["normalized_position"],
        "location": selected["location"],
        "confidence": round(max(0.0, min(0.99, 0.60 + selected["selection_score"] * 0.30 - (0.15 if ambiguous else 0.0))), 4),
        "exclusion_bbox_world": _expand(selected["bbox_world"], exclusion_margin),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "unit_conversion_note": "比例尺的纸张/模型单位换算仅在 DWG drawing_units 与纸张单位一致时成立；模型仍需保留原始世界坐标。",
    }
    return evidence
