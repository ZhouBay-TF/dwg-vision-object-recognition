from __future__ import annotations

"""Deterministic checks that sit after model inference."""

import math
from collections import defaultdict
import re
from typing import Any, Iterable

from .reconcile import area, iou


LABEL_TYPES: tuple[tuple[str, str], ...] = (
    ("window", "window"),
    ("窗", "window"),
    ("door", "door"),
    ("门", "door"),
    ("入口", "door"),
    ("bed", "furniture"),
    ("床", "furniture"),
    ("sofa", "furniture"),
    ("沙发", "furniture"),
    ("chair", "furniture"),
    ("椅", "furniture"),
    ("table", "furniture"),
    ("桌", "furniture"),
    ("cabinet", "furniture"),
    ("柜", "furniture"),
    ("toilet", "furniture"),
    ("马桶", "furniture"),
    ("洗手盆", "furniture"),
    ("淋浴", "furniture"),
    ("sanitary", "furniture"),
)


def _label_type(text: str) -> str | None:
    lowered = text.lower()
    for term, entity_type in LABEL_TYPES:
        if term.lower() in lowered:
            return entity_type
    return None


def _label_bbox(annotation: dict[str, Any]) -> list[float] | None:
    value = annotation.get("bbox_px") or annotation.get("bbox_world")
    if not value or len(value) != 4:
        return None
    return [float(item) for item in value]


def _issue(severity: str, code: str, message: str, entity_id: str | None = None, **details: Any) -> dict[str, Any]:
    result = {"severity": severity, "code": code, "message": message}
    if entity_id:
        result["entity_id"] = entity_id
    if details:
        result["details"] = details
    return result


def validate_entities(
    entities: list[dict[str, Any]],
    *,
    annotations: Iterable[dict[str, Any]] = (),
    page_width: int | float | None = None,
    page_height: int | float | None = None,
    disagreement_notes: list[str] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    annotation_list = list(annotations)

    for index, entity in enumerate(entities):
        entity_id = str(entity.get("id") or f"entity_{index:04d}")
        entity["id"] = entity_id
        bbox = entity.get("bbox_px") or []
        geometry_mode = str(entity.get("geometry_mode") or "area").lower()
        try:
            bbox_values = [float(value) for value in bbox]
        except (TypeError, ValueError):
            bbox_values = []
        linear_bbox = (
            len(bbox_values) == 4
            and geometry_mode == "linear"
            and all(math.isfinite(value) for value in bbox_values)
            and max(abs(bbox_values[2] - bbox_values[0]), abs(bbox_values[3] - bbox_values[1])) > 1e-9
        )
        if len(bbox_values) != 4 or (area(bbox_values) <= 0 and not linear_bbox):
            issues.append(_issue("error", "invalid_bbox", "实体边界框为空或面积为零", entity_id))
            continue
        if not all(math.isfinite(value) for value in bbox_values):
            issues.append(_issue("error", "non_finite_bbox", "实体边界框包含非有限数字", entity_id))
        confidence = float(entity.get("confidence", 0.0) or 0.0)
        if confidence < 0.45:
            issues.append(_issue("warning", "low_confidence", "实体置信度较低，需要人工复核", entity_id, confidence=confidence))
        if page_width is not None and page_height is not None:
            if bbox[0] < 0 or bbox[1] < 0 or bbox[2] > page_width or bbox[3] > page_height:
                issues.append(_issue("warning", "out_of_canvas", "实体边界框超出页面范围，已由程序截断或需要复核", entity_id))

        evidence_text = " ".join(
            [
                str(entity.get("subtype") or ""),
                str(entity.get("evidence", {}).get("visual_summary") or ""),
                " ".join(str(item) for item in entity.get("evidence", {}).get("nearby_text", []) or []),
            ]
        )
        expected_from_evidence = _label_type(evidence_text)
        if expected_from_evidence and expected_from_evidence != entity.get("type"):
            issues.append(
                _issue(
                    "warning",
                    "label_type_mismatch",
                    "对象类别与对象附近/证据中的明显标注不一致",
                    entity_id,
                    expected_type=expected_from_evidence,
                    actual_type=entity.get("type"),
                    evidence=evidence_text,
                )
            )

        for annotation in annotation_list:
            text = str(annotation.get("text") or "").strip()
            expected_type = _label_type(text)
            label_bbox = _label_bbox(annotation)
            if not text or not expected_type or not label_bbox:
                continue
            if iou([float(value) for value in bbox], label_bbox) <= 0:
                # Text is often a point-sized box.  A center-distance check is
                # more useful than IoU for a nearby label.
                entity_center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
                label_center = ((label_bbox[0] + label_bbox[2]) / 2.0, (label_bbox[1] + label_bbox[3]) / 2.0)
                distance = math.dist(entity_center, label_center)
                scale = max(1.0, min(bbox[2] - bbox[0], bbox[3] - bbox[1]))
                if distance > scale * 1.4:
                    continue
            if expected_type != entity.get("type"):
                issues.append(
                    _issue(
                        "error" if text in {"门", "窗", "door", "window"} else "warning",
                        "nearby_label_mismatch",
                        "对象与相邻标注文字的类别不一致",
                        entity_id,
                        label=text,
                        expected_type=expected_type,
                        actual_type=entity.get("type"),
                    )
                )

    # Two different classes at the same location are a useful review trigger,
    # but not automatically an error: a block can contain a door/window symbol.
    # Use a coarse spatial index because vector exports can contain thousands
    # of wall segments.
    bucket_size = 512.0
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, entity in enumerate(entities):
        box = entity.get("bbox_px", [0, 0, 0, 0])
        center = ((float(box[0]) + float(box[2])) / 2.0, (float(box[1]) + float(box[3])) / 2.0)
        buckets[(math.floor(center[0] / bucket_size), math.floor(center[1] / bucket_size))].append(index)
    seen_pairs: set[tuple[int, int]] = set()
    for index, first in enumerate(entities):
        box = first.get("bbox_px", [0, 0, 0, 0])
        center = ((float(box[0]) + float(box[2])) / 2.0, (float(box[1]) + float(box[3])) / 2.0)
        cell_x, cell_y = math.floor(center[0] / bucket_size), math.floor(center[1] / bucket_size)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other_index in buckets.get((cell_x + dx, cell_y + dy), []):
                    if other_index <= index:
                        continue
                    second = entities[other_index]
                    if first.get("type") == second.get("type"):
                        continue
                    pair = (index, other_index)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    if iou(first.get("bbox_px", [0, 0, 0, 0]), second.get("bbox_px", [0, 0, 0, 0])) >= 0.70:
                        issues.append(
                            _issue(
                                "warning",
                                "cross_type_overlap",
                                "不同类别对象的边界框高度重叠，可能是重复识别或复合符号",
                                first.get("id"),
                                other_entity_id=second.get("id"),
                            )
                        )

    for note in disagreement_notes or []:
        issues.append(_issue("warning", "model_disagreement", note))

    summary = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        summary[issue["severity"]] = summary.get(issue["severity"], 0) + 1
    return {
        "status": "error" if summary["error"] else ("warning" if summary["warning"] else "ok"),
        "issues": issues,
        "summary": summary,
        "rules": [
            "bbox_non_empty_and_finite",
            "page_bounds",
            "confidence_threshold_review",
            "nearby_label_semantic_consistency",
            "cross_type_overlap_review",
        ],
    }
