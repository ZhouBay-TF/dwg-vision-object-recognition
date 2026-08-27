from __future__ import annotations

"""Shared data contracts for whole-drawing detection.

The project intentionally keeps the wire format as plain dictionaries.  That
makes the JSON output usable from Python, C#, AutoLISP and downstream BIM
tools without requiring a generated client.
"""

from typing import Any, Literal


ENTITY_TYPES = ("wall", "window", "door", "furniture")
EntityType = Literal["wall", "window", "door", "furniture"]


def empty_detection_document(*, source: str = "") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source": {"path": source, "format": ""},
        "coordinate_system": {
            "space": "pixel",
            "units": "unknown",
            "origin": "top_left",
            "world_bounds": None,
        },
        "entities": [],
        "annotations": [],
        "model_runs": [],
        "validation": {
            "status": "not_run",
            "issues": [],
            "summary": {"error": 0, "warning": 0, "info": 0},
        },
        "audit": {
            "decision_policy": "evidence-first; model outputs are reconciled with geometry and labels",
            "reasoning_disclosure": "structured evidence and checks only; hidden chain of thought is not stored",
        },
    }


def entity_schema() -> dict[str, Any]:
    """Return a JSON Schema suitable for API responses and CI validation."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": list(ENTITY_TYPES)},
                        "subtype": {"type": "string"},
                        "bbox_px": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "polygon_px": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                        },
                        "rotation_deg": {"type": "number"},
                        "dimensions": {"type": "object"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {"type": "object"},
                    },
                    "required": ["type", "subtype", "bbox_px", "rotation_deg", "dimensions", "confidence", "evidence"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["entities", "notes"],
    }


def normalize_entity(entity: dict[str, Any], *, default_source: str = "model") -> dict[str, Any]:
    """Normalize a model/vector candidate into the public entity shape."""
    entity_type = str(entity.get("type", "")).lower().strip()
    aliases = {
        "墙": "wall",
        "墙体": "wall",
        "窗": "window",
        "门": "door",
        "家具": "furniture",
        "wall": "wall",
        "window": "window",
        "door": "door",
        "furniture": "furniture",
    }
    entity_type = aliases.get(entity_type, entity_type)
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unsupported entity type: {entity_type!r}")

    raw_bbox = entity.get("bbox_px") or entity.get("bbox") or [0, 0, 0, 0]
    if len(raw_bbox) != 4:
        raise ValueError("bbox_px must contain four numbers")
    bbox = [float(value) for value in raw_bbox]
    left, top, right, bottom = bbox
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top

    polygon = entity.get("polygon_px") or []
    normalized_polygon = [[float(point[0]), float(point[1])] for point in polygon if len(point) >= 2]
    dimensions = dict(entity.get("dimensions") or {})
    dimensions.setdefault("width_px", max(0.0, right - left))
    dimensions.setdefault("height_px", max(0.0, bottom - top))

    evidence = dict(entity.get("evidence") or {})
    if entity.get("evidence_text"):
        evidence.setdefault("visual_summary", str(entity["evidence_text"]))
    evidence.setdefault("source", default_source)

    return {
        "id": str(entity.get("id") or ""),
        "type": entity_type,
        "subtype": str(entity.get("subtype") or "unknown"),
        "bbox_px": [left, top, right, bottom],
        "polygon_px": normalized_polygon,
        "rotation_deg": float(entity.get("rotation_deg", 0.0) or 0.0),
        "dimensions": dimensions,
        "confidence": max(0.0, min(1.0, float(entity.get("confidence", 0.0) or 0.0))),
        "evidence": evidence,
        "provenance": dict(entity.get("provenance") or {"source": default_source}),
    }
