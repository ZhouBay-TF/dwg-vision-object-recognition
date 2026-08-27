from __future__ import annotations

from dwg_vision.scene import build_scene, detect_frames
from dwg_vision.scene_graph import build_scene_graph


def _scene() -> dict:
    raw = {
        "coordinate_system": {"world_bounds": [0, 0, 500, 500]},
        "frames": [{"scene_id": "scene_1", "world_bbox": [0, 0, 500, 500]}],
        "entities": [{
            "entity_id": "ent_1", "handle": "A1", "type": "furniture", "subtype": "toilet",
            "bbox_world": [100, 100, 160, 160], "polygon_world": [[100, 100], [160, 100], [160, 160], [100, 160]],
        }],
        "annotations": [{"text_id": "txt_1", "text": "坐便器", "role": "equipment_label", "bbox_world": [90, 80, 170, 95]}],
    }
    return build_scene(raw, detect_frames(raw)[0])


def test_native_equipment_text_has_semantic_priority() -> None:
    scene = _scene()
    primitive_id = scene["primitives"][0]["primitive_id"]
    graph = build_scene_graph(
        scene,
        {
            "schema_version": "sympoint_result.v1",
            "scene_id": "scene_1",
            "instances": [{
                "instance_id": "spv_1", "class_name": "toilet", "score": 0.8,
                "primitive_ids": [primitive_id],
            }],
            "semantic_by_primitive": [],
        },
    )
    assert graph["schema_version"] == "scene_graph.v1"
    assert graph["objects"][0]["subtype"] == "toilet"
    assert graph["objects"][0]["semantic_source"] == "native_text"
    assert graph["objects"][0]["source_handles"] == ["A1"]


def test_dimension_values_do_not_pollute_nearby_text_evidence() -> None:
    scene = _scene()
    scene["texts"].append({
        "text_id": "dim_1", "text": "18000", "role": "dimension",
        "bbox_world": [90, 80, 170, 95],
    })
    primitive_id = scene["primitives"][0]["primitive_id"]
    graph = build_scene_graph(
        scene,
        {
            "schema_version": "sympoint_result.v1",
            "scene_id": "scene_1",
            "instances": [{
                "instance_id": "spv_1", "class_name": "toilet", "score": 0.8,
                "primitive_ids": [primitive_id],
            }],
            "semantic_by_primitive": [],
        },
    )
    assert "18000" not in graph["objects"][0]["evidence"]["nearby_text"]


def test_text_model_conflict_is_retained_for_review() -> None:
    scene = _scene()
    primitive_id = scene["primitives"][0]["primitive_id"]
    graph = build_scene_graph(
        scene,
        {
            "schema_version": "sympoint_result.v1",
            "scene_id": "scene_1",
            "instances": [{
                "instance_id": "spv_1", "class_name": "sink", "score": 0.8,
                "primitive_ids": [primitive_id],
            }],
            "semantic_by_primitive": [],
        },
    )
    assert graph["objects"][0]["subtype"] == "toilet"
    assert graph["objects"][0]["status"] == "review"
    assert graph["objects"][0]["evidence"]["conflicts"][0]["code"] == "TEXT_MODEL_CONFLICT"


def test_low_confidence_candidate_is_not_confirmed() -> None:
    scene = _scene()
    # With no authoritative native equipment label, a low SymPointV2 score
    # must remain a review candidate.  Direct text is intentionally allowed to
    # raise confidence elsewhere in the graph.
    scene["texts"] = []
    primitive_id = scene["primitives"][0]["primitive_id"]
    graph = build_scene_graph(
        scene,
        {
            "schema_version": "sympoint_result.v1",
            "scene_id": "scene_1",
            "instances": [{
                "instance_id": "spv_low", "class_name": "toilet", "score": 0.12,
                "primitive_ids": [primitive_id],
            }],
            "semantic_by_primitive": [],
        },
    )
    assert graph["objects"][0]["status"] == "review"
    assert not any(item["code"] == "semantic_conflict_review" for item in graph["validation"]["issues"])


def test_sympoint_class_id_and_primitive_index_are_resolved() -> None:
    scene = _scene()
    graph = build_scene_graph(
        scene,
        {
            "schema_version": "sympoint_result.v1",
            "scene_id": "scene_1",
            "instances": [{"instance_id": "spv_1", "class_id": 26, "score": 0.8, "primitive_indices": [0]}],
            "semantic_by_primitive": [],
        },
    )
    assert graph["objects"][0]["subtype"] == "toilet"
    assert graph["objects"][0]["primitive_ids"] == [scene["primitives"][0]["primitive_id"]]


def test_linear_cad_geometry_is_not_invalid_bbox() -> None:
    raw = {
        "coordinate_system": {"world_bounds": [0, 0, 500, 500]},
        "frames": [{"scene_id": "scene_line", "world_bbox": [0, 0, 500, 500]}],
        "entities": [{
            "entity_id": "wall_1", "handle": "W1", "type": "wall", "subtype": "wall",
            "bbox_world": [100, 200, 400, 200],
            "polygon_world": [],
            "points_world": [[100, 200], [400, 200]],
        }],
        "annotations": [],
    }
    scene = build_scene(raw, detect_frames(raw)[0])
    primitive_id = scene["primitives"][0]["primitive_id"]
    graph = build_scene_graph(
        scene,
        {
            "schema_version": "sympoint_result.v1",
            "scene_id": "scene_line",
            "instances": [{"instance_id": "spv_wall", "class_name": "wall", "score": 0.8, "primitive_ids": [primitive_id]}],
            "semantic_by_primitive": [],
        },
    )
    assert graph["objects"][0]["geometry_mode"] == "linear"
    assert graph["validation"]["summary"]["error"] == 0
