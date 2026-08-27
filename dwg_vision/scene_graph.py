from __future__ import annotations

"""Deterministic candidate fusion and Scene Graph submission."""

import math
import re
from collections import defaultdict
from typing import Any, Iterable

from .contracts import SCHEMA_VERSIONS, require_bbox
from .validation import validate_entities


SYMPPOINT_CLASS_NAMES = {
    0: "single_door",
    1: "double_door",
    2: "sliding_door",
    3: "folding_door",
    4: "revolving_door",
    5: "rolling_door",
    6: "window",
    7: "bay_window",
    8: "blind_window",
    9: "opening_symbol",
    10: "sofa",
    11: "bed",
    12: "chair",
    13: "table",
    14: "tv_cabinet",
    15: "wardrobe",
    16: "cabinet",
    17: "gas_stove",
    18: "sink",
    19: "refrigerator",
    20: "airconditioner",
    21: "bath",
    22: "bath_tub",
    23: "washing_machine",
    24: "squat_toilet",
    25: "urinal",
    26: "toilet",
    27: "stairs",
    28: "elevator",
    29: "escalator",
    30: "row_chairs",
    31: "parking_spot",
    32: "wall",
    33: "curtain_wall",
    34: "railing",
}

# Keep the public graph conservative: a low score may still be useful for
# recall and review, but must not look like an engineering-confirmed object.
REVIEW_CONFIDENCE_THRESHOLD = 0.45


def _bbox_union(boxes: Iterable[list[float]]) -> list[float]:
    values = [require_bbox(box) for box in boxes]
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(box[0] for box in values),
        min(box[1] for box in values),
        max(box[2] for box in values),
        max(box[3] for box in values),
    ]


def _transform_point(point: Iterable[float], transform: dict[str, Any]) -> list[float]:
    origin = transform.get("origin") or [0.0, 0.0]
    scale = float(transform.get("scale", 1.0) or 1.0)
    rotation = math.radians(float(transform.get("rotation_deg", 0.0) or 0.0))
    x, y = [float(value) for value in point][:2]
    x *= scale
    y *= scale
    cosine, sine = math.cos(rotation), math.sin(rotation)
    return [float(origin[0]) + x * cosine - y * sine, float(origin[1]) + x * sine + y * cosine]


def local_bbox_to_world(local_bbox: Iterable[float], scene: dict[str, Any]) -> list[float]:
    box = require_bbox(local_bbox, "local bbox")
    corners = [
        _transform_point((box[0], box[1]), scene["local_to_world"]),
        _transform_point((box[2], box[1]), scene["local_to_world"]),
        _transform_point((box[2], box[3]), scene["local_to_world"]),
        _transform_point((box[0], box[3]), scene["local_to_world"]),
    ]
    return _bbox_union([[p[0], p[1], p[0], p[1]] for p in corners])


def _class_type(class_name: str) -> str | None:
    lowered = str(class_name or "").lower()
    if any(term in lowered for term in ("wall", "curtain wall", "railing")):
        return "wall"
    if any(term in lowered for term in ("window", "blind window", "bay window", "opening")):
        return "window"
    if "door" in lowered:
        return "door"
    if any(term in lowered for term in (
        "sofa", "bed", "chair", "table", "cabinet", "wardrobe", "sink", "toilet", "bath",
        "tub", "stove", "refrigerator", "washing", "urinal", "furniture", "tv", "airconditioner",
        "家具", "洁具",
    )):
        return "furniture"
    return None


def _subtype_from_text(text: str) -> str | None:
    value = str(text or "").strip().lower()
    terms: tuple[tuple[tuple[str, ...], str], ...] = (
        (("壁挂坐便", "壁挂马桶", "wall hung toilet", "wall-hung toilet"), "wall_hung_toilet"),
        (("坐便器", "坐便", "马桶", "toilet", "wc"), "toilet"),
        (("蹲便", "蹲厕", "squat toilet"), "squat_toilet"),
        (("小便器", "小便斗", "urinal"), "urinal"),
        (("洗手盆", "洗脸盆", "台盆", "sink", "basin"), "sink"),
        (("浴缸", "bathtub", "bath tub"), "bath_tub"),
        (("淋浴", "shower"), "shower"),
        (("床", "bed"), "bed"),
        (("沙发", "sofa"), "sofa"),
        (("餐桌", "table", "桌"), "table"),
        (("椅", "chair"), "chair"),
        (("衣柜", "wardrobe"), "wardrobe"),
        (("柜", "cabinet"), "cabinet"),
        (("冰箱", "refrigerator"), "refrigerator"),
    )
    for candidates, subtype in terms:
        if any(term in value for term in candidates):
            return subtype
    return None


def _candidate_texts(candidate_bbox: list[float], texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    center = ((candidate_bbox[0] + candidate_bbox[2]) / 2.0, (candidate_bbox[1] + candidate_bbox[3]) / 2.0)
    scale = max(1.0, min(candidate_bbox[2] - candidate_bbox[0], candidate_bbox[3] - candidate_bbox[1]))
    max_distance = max(scale * 2.5, 300.0)
    selected: list[dict[str, Any]] = []
    for text in texts:
        # DIMENSION values are retained in the native evidence stream and are
        # used by dedicated dimension checks, but a raw number is not an
        # equipment label.  Feeding every dimension into nearby-text fusion
        # causes one object to inherit hundreds of unrelated measurements.
        if text.get("role") == "dimension":
            continue
        bbox = text.get("bbox_world") or []
        if len(bbox) != 4:
            continue
        text_center = ((float(bbox[0]) + float(bbox[2])) / 2.0, (float(bbox[1]) + float(bbox[3])) / 2.0)
        distance = math.dist(center, text_center)
        if distance <= max_distance or text.get("role") == "equipment_label":
            selected.append({**text, "distance_world": distance})
    return sorted(selected, key=lambda item: float(item.get("distance_world", 0.0)))


def _primitive_lookup(scene: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["primitive_id"]): item for item in scene.get("primitives", [])}


def _class_name(value: dict[str, Any]) -> str:
    explicit = str(value.get("class_name") or value.get("class") or "").strip()
    if explicit:
        return explicit
    try:
        return SYMPPOINT_CLASS_NAMES.get(int(value.get("class_id")), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def _primitive_ids(value: dict[str, Any], scene: dict[str, Any]) -> list[str]:
    primitives = scene.get("primitives") or []
    ids = [str(item) for item in (value.get("primitive_ids") or [])]
    if ids:
        return ids
    result: list[str] = []
    indices = list(value.get("primitive_indices") or [])
    if value.get("primitive_index") is not None:
        indices.append(value.get("primitive_index"))
    for index in indices:
        try:
            primitive = primitives[int(index)]
        except (IndexError, TypeError, ValueError):
            continue
        if isinstance(primitive, dict) and primitive.get("primitive_id"):
            result.append(str(primitive["primitive_id"]))
    return result


def _instance_bbox(instance: dict[str, Any], scene: dict[str, Any], primitives: dict[str, dict[str, Any]]) -> list[float]:
    if instance.get("bbox_world"):
        return require_bbox(instance["bbox_world"], "instance.bbox_world")
    if instance.get("bbox_local"):
        return local_bbox_to_world(instance["bbox_local"], scene)
    boxes: list[list[float]] = []
    for primitive_id in _primitive_ids(instance, scene):
        primitive = primitives.get(str(primitive_id))
        if primitive:
            boxes.append(require_bbox(primitive.get("bbox_world") or local_bbox_to_world(primitive["bbox_local"], scene)))
    if boxes:
        return _bbox_union(boxes)
    return [0.0, 0.0, 0.0, 0.0]


def _geometry_mode(
    bbox: list[float],
    primitive_ids: Iterable[str],
    primitives: dict[str, dict[str, Any]],
) -> str:
    """Classify whether a candidate is area-, line-, or point-like.

    CAD walls are frequently represented by a single line or a set of
    parallel lines.  Their axis-aligned bbox can therefore have zero area
    even though the underlying geometry is valid and has non-zero length.
    Keeping this distinction prevents the deterministic validator from
    treating normal wall geometry as a malformed object.
    """
    width = abs(float(bbox[2]) - float(bbox[0])) if len(bbox) == 4 else 0.0
    height = abs(float(bbox[3]) - float(bbox[1])) if len(bbox) == 4 else 0.0
    if width > 1e-9 and height > 1e-9:
        return "area"
    for primitive_id in primitive_ids:
        primitive = primitives.get(str(primitive_id)) or {}
        points = primitive.get("points_local") or primitive.get("points_world") or []
        for first, second in zip(points, points[1:]):
            try:
                if math.dist([float(first[0]), float(first[1])], [float(second[0]), float(second[1])]) > 1e-9:
                    return "linear"
            except (IndexError, TypeError, ValueError):
                continue
    return "point"


def _observations_for(instance_id: str, object_id: str, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in observations
        if str(item.get("object_id") or item.get("instance_id") or "") in {instance_id, object_id}
    ]


def _best_visual_subtype(observations: list[dict[str, Any]]) -> tuple[str | None, float]:
    votes: dict[str, float] = defaultdict(float)
    for observation in observations:
        category = str(observation.get("category") or observation.get("subtype") or "")
        if not category:
            continue
        subtype = _subtype_from_text(category) or re.sub(r"[^a-z0-9_]+", "_", category.lower()).strip("_")
        votes[subtype] += max(0.0, min(1.0, float(observation.get("confidence", 0.0) or 0.0)))
    if not votes:
        return None, 0.0
    subtype, weight = max(votes.items(), key=lambda item: item[1])
    return subtype, min(0.99, weight / max(1.0, len(observations)))


def build_scene_graph(
    scene: dict[str, Any],
    sympoint_result: dict[str, Any],
    *,
    observations: Iterable[dict[str, Any]] = (),
    texts: Iterable[dict[str, Any]] | None = None,
    scene_overview: dict[str, Any] | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    """Fuse stable SymPoint candidates with text and optional visual evidence.

    Geometry and entity identity come from the Scene.  Text has semantic
    priority for direct equipment labels, while visual observations can refine
    subtypes when no authoritative label exists.
    """
    primitive_lookup = _primitive_lookup(scene)
    text_list = list(texts if texts is not None else scene.get("texts", []))
    observation_list = list(observations)
    objects: list[dict[str, Any]] = []
    used_primitive_ids: set[str] = set()
    unmapped_model_classes: list[dict[str, Any]] = []

    instances = list(sympoint_result.get("instances") or [])
    for index, instance in enumerate(instances):
        instance_id = str(instance.get("instance_id") or f"spv_{index:04d}")
        primitive_ids = [item for item in _primitive_ids(instance, scene) if item in primitive_lookup]
        if not primitive_ids:
            continue
        class_name = _class_name(instance)
        entity_type = _class_type(class_name)
        if entity_type is None:
            # The public contract currently exposes wall/window/door/furniture.
            # Do not silently turn stairs, elevators or parking spots into
            # furniture; retain the model class in provenance for audit.
            unmapped_model_classes.append({
                "instance_id": instance_id,
                "class_name": class_name,
                "primitive_ids": primitive_ids,
            })
            continue
        used_primitive_ids.update(primitive_ids)
        bbox_world = _instance_bbox(instance, scene, primitive_lookup)
        geometry_mode = _geometry_mode(bbox_world, primitive_ids, primitive_lookup)
        related_texts = _candidate_texts(bbox_world, text_list)
        direct_texts = [item for item in related_texts if item.get("role") == "equipment_label"]
        text_subtypes = [_subtype_from_text(item.get("text", "")) for item in direct_texts]
        text_subtypes = [item for item in text_subtypes if item]
        object_id = f"obj_{scene['scene_id']}_{index:04d}"
        object_observations = _observations_for(instance_id, object_id, observation_list)
        visual_subtype, visual_confidence = _best_visual_subtype(object_observations)
        model_subtype = _subtype_from_text(class_name) or re.sub(r"[^a-z0-9_]+", "_", class_name.lower()).strip("_") or "unknown"
        if text_subtypes:
            subtype = text_subtypes[0]
            semantic_source = "native_text"
        elif visual_subtype:
            subtype = visual_subtype
            semantic_source = "visual_observation"
        else:
            subtype = model_subtype
            semantic_source = "sympointv2"
        conflicts: list[dict[str, Any]] = []
        if text_subtypes and model_subtype not in {"unknown", subtype}:
            conflicts.append({"code": "TEXT_MODEL_CONFLICT", "model_subtype": model_subtype, "text_subtype": subtype})
        if visual_subtype and text_subtypes and visual_subtype != subtype:
            conflicts.append({"code": "TEXT_VISION_CONFLICT", "visual_subtype": visual_subtype, "text_subtype": subtype})
        source_handles = sorted({str(primitive_lookup[item].get("handle") or "") for item in primitive_ids if primitive_lookup[item].get("handle")})
        evidence_refs = [instance_id]
        evidence_refs.extend(str(item.get("text_id")) for item in related_texts if item.get("text_id"))
        evidence_refs.extend(str(item.get("observation_id")) for item in object_observations if item.get("observation_id"))
        confidence = float(instance.get("score", instance.get("confidence", 0.0)) or 0.0)
        confidence = max(confidence, visual_confidence if visual_subtype else 0.0)
        if text_subtypes:
            confidence = min(0.99, max(confidence, 0.88))
        object_status = "review" if conflicts or confidence < REVIEW_CONFIDENCE_THRESHOLD else "confirmed"
        objects.append({
            "object_id": object_id,
            "scene_id": scene["scene_id"],
            "type": entity_type,
            "subtype": subtype,
            "geometry_mode": geometry_mode,
            "geometry_world": {"bbox": bbox_world, "polygon": []},
            "source_handles": source_handles,
            "source_entity_ids": sorted({str(primitive_lookup[item].get("source_entity_id") or "") for item in primitive_ids}),
            "primitive_ids": primitive_ids,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "status": object_status,
            "semantic_source": semantic_source,
            "evidence_refs": evidence_refs,
            "evidence": {
                "sympoint_class": class_name,
                "nearby_text": [item.get("text", "") for item in related_texts],
                "visual_observations": object_observations,
                "conflicts": conflicts,
            },
        })

    # If a stuff class has no instance prediction, retain primitive-level
    # semantic candidates instead of dropping walls from the final graph.
    for semantic in sympoint_result.get("semantic_by_primitive") or []:
        semantic_ids = _primitive_ids(semantic, scene)
        primitive_id = semantic_ids[0] if semantic_ids else ""
        if not primitive_id or primitive_id in used_primitive_ids or primitive_id not in primitive_lookup:
            continue
        class_name = _class_name(semantic)
        entity_type = _class_type(class_name)
        if entity_type is None:
            continue
        primitive = primitive_lookup[primitive_id]
        bbox_world = require_bbox(primitive.get("bbox_world") or local_bbox_to_world(primitive["bbox_local"], scene))
        geometry_mode = _geometry_mode(bbox_world, [primitive_id], primitive_lookup)
        objects.append({
            "object_id": f"obj_{scene['scene_id']}_primitive_{len(objects):04d}",
            "scene_id": scene["scene_id"],
            "type": entity_type,
            "subtype": _subtype_from_text(class_name) or class_name,
            "geometry_mode": geometry_mode,
            "geometry_world": {"bbox": bbox_world, "polygon": []},
            "source_handles": [str(primitive.get("handle"))] if primitive.get("handle") else [],
            "source_entity_ids": [str(primitive.get("source_entity_id"))],
            "primitive_ids": [primitive_id],
            "confidence": max(0.0, min(1.0, float(semantic.get("score", 0.0) or 0.0))),
            "status": "review" if float(semantic.get("score", 0.0) or 0.0) < REVIEW_CONFIDENCE_THRESHOLD else "confirmed",
            "semantic_source": "sympointv2_primitive",
            "evidence_refs": [primitive_id],
            "evidence": {"sympoint_class": class_name, "nearby_text": [], "visual_observations": [], "conflicts": []},
        })

    validation_entities = [
        {
            "id": item["object_id"],
            "type": item["type"],
            "subtype": item["subtype"],
            "geometry_mode": item["geometry_mode"],
            "bbox_px": [
                item["geometry_world"]["bbox"][0] - scene["world_bounds"][0],
                item["geometry_world"]["bbox"][1] - scene["world_bounds"][1],
                item["geometry_world"]["bbox"][2] - scene["world_bounds"][0],
                item["geometry_world"]["bbox"][3] - scene["world_bounds"][1],
            ],
            "confidence": item["confidence"],
            "evidence": {
                "visual_summary": item["subtype"],
                "nearby_text": item["evidence"].get("nearby_text", []),
            },
        }
        for item in objects
    ]
    validation_texts: list[dict[str, Any]] = []
    for text in text_list:
        copied = dict(text)
        if copied.get("bbox_world") and len(copied["bbox_world"]) == 4:
            copied["bbox_px"] = [
                float(copied["bbox_world"][0]) - scene["world_bounds"][0],
                float(copied["bbox_world"][1]) - scene["world_bounds"][1],
                float(copied["bbox_world"][2]) - scene["world_bounds"][0],
                float(copied["bbox_world"][3]) - scene["world_bounds"][1],
            ]
        validation_texts.append(copied)
    validation = validate_entities(
        validation_entities,
        annotations=validation_texts,
        page_width=scene["local_bounds"][2],
        page_height=scene["local_bounds"][3],
    )
    for item in objects:
        if item["status"] == "review" and item["evidence"].get("conflicts"):
            validation["status"] = "warning" if validation["status"] == "ok" else validation["status"]
            validation["issues"].append({
                "severity": "warning",
                "code": "semantic_conflict_review",
                "message": "文字、模型或视觉证据存在语义冲突，需要人工复核",
                "entity_id": item["object_id"],
                "details": item["evidence"]["conflicts"],
            })
    validation["summary"] = {"error": 0, "warning": 0, "info": 0}
    for issue in validation["issues"]:
        validation["summary"][issue["severity"]] = validation["summary"].get(issue["severity"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSIONS["graph"],
        "job_id": job_id,
        "scene_id": scene["scene_id"],
        "coordinate_space": "world",
        "world_bounds": scene["world_bounds"],
        "objects": objects,
        "scene_overview": scene_overview,
        "validation": validation,
        "provenance": {
            "sympoint_schema": sympoint_result.get("schema_version", ""),
            "sympoint": sympoint_result.get("provenance", {}),
            "text_priority": "direct equipment labels override coarse model subtype; conflicts are retained",
            "unmapped_model_classes": unmapped_model_classes,
        },
    }
