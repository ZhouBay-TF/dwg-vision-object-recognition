from __future__ import annotations

"""Versioned contracts shared by the local DWG pipeline and cloud API.

The contracts intentionally remain JSON-compatible dictionaries.  The local
AutoCAD/C# exporter and the remote Python worker can therefore exchange data
without sharing a runtime or generated client code.
"""

from typing import Any


SCHEMA_VERSIONS = {
    "raw": "dwg_raw.v1",
    "scene": "dwg_scene.v1",
    "job": "sympoint_job.v1",
    "sympoint": "sympoint_result.v1",
    "observation": "visual_observation.v1",
    "scene_overview": "visual_scene_overview.v1",
    "graph": "scene_graph.v1",
}


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def require_bbox(value: Any, name: str = "bbox") -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{name} must contain four numbers")
    bbox = [float(item) for item in value]
    if bbox[2] < bbox[0]:
        bbox[0], bbox[2] = bbox[2], bbox[0]
    if bbox[3] < bbox[1]:
        bbox[1], bbox[3] = bbox[3], bbox[1]
    return bbox


def validate_scene(scene: dict[str, Any]) -> None:
    require_mapping(scene, "scene")
    if scene.get("schema_version") != SCHEMA_VERSIONS["scene"]:
        raise ValueError(f"scene schema must be {SCHEMA_VERSIONS['scene']}")
    scene_id = str(scene.get("scene_id") or "")
    if not scene_id:
        raise ValueError("scene.scene_id is required")
    require_bbox(scene.get("world_bounds"), "scene.world_bounds")
    require_bbox(scene.get("local_bounds"), "scene.local_bounds")
    primitives = scene.get("primitives")
    if not isinstance(primitives, list):
        raise ValueError("scene.primitives must be an array")
    primitive_ids: set[str] = set()
    for index, primitive in enumerate(primitives):
        require_mapping(primitive, f"scene.primitives[{index}]")
        primitive_id = str(primitive.get("primitive_id") or "")
        if not primitive_id or primitive_id in primitive_ids:
            raise ValueError(f"primitive_id missing or duplicated at index {index}")
        primitive_ids.add(primitive_id)
        points = primitive.get("points_local")
        if not isinstance(points, list) or not points:
            raise ValueError(f"primitive {primitive_id} has no points_local")
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise ValueError(f"primitive {primitive_id} contains an invalid point")


def validate_job_manifest(manifest: dict[str, Any]) -> None:
    require_mapping(manifest, "job_manifest")
    if manifest.get("schema_version") != SCHEMA_VERSIONS["job"]:
        raise ValueError(f"job schema must be {SCHEMA_VERSIONS['job']}")
    if not str(manifest.get("job_id") or ""):
        raise ValueError("job_manifest.job_id is required")
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("job_manifest.scenes must be a non-empty array")
    scene_ids: set[str] = set()
    for scene in scenes:
        require_mapping(scene, "job_manifest.scenes[]")
        scene_id = str(scene.get("scene_id") or "")
        if not scene_id or scene_id in scene_ids:
            raise ValueError("scene_id is missing or duplicated in job manifest")
        scene_ids.add(scene_id)
        if not str(scene.get("path") or ""):
            raise ValueError(f"scene {scene_id} has no path")


def validate_raw(raw: dict[str, Any]) -> None:
    """Validate the minimum AutoCAD export contract before Scene splitting."""
    require_mapping(raw, "dwg_raw")
    if raw.get("schema_version") not in {None, SCHEMA_VERSIONS["raw"], "1.0"}:
        raise ValueError(f"unsupported raw schema: {raw.get('schema_version')!r}")
    entities = raw.get("raw_entities") or raw.get("entities") or []
    annotations = raw.get("annotations") or raw.get("texts") or []
    if not isinstance(entities, list):
        raise ValueError("dwg_raw.raw_entities/entities must be an array")
    if not isinstance(annotations, list):
        raise ValueError("dwg_raw.annotations/texts must be an array")
    for index, entity in enumerate(entities):
        require_mapping(entity, f"dwg_raw.entities[{index}]")
        if not str(entity.get("entity_id") or entity.get("id") or entity.get("handle") or ""):
            raise ValueError(f"dwg_raw entity {index} has no stable entity_id/handle")
        if entity.get("bbox_world") is not None:
            require_bbox(entity["bbox_world"], f"dwg_raw entity {index}.bbox_world")
    for index, annotation in enumerate(annotations):
        require_mapping(annotation, f"dwg_raw.annotations[{index}]")
        if annotation.get("bbox_world") is not None:
            require_bbox(annotation["bbox_world"], f"dwg_raw annotation {index}.bbox_world")


def normalize_source(source: dict[str, Any] | None, *, filename: str = "") -> dict[str, Any]:
    value = dict(source or {})
    value.setdefault("filename", filename)
    value.setdefault("sha256", "")
    return value
