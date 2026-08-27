from __future__ import annotations

import json

from PIL import Image

from dwg_vision.comparison import generate_scene_comparison


def test_comparison_writes_source_layers_with_three_provenance_counts(tmp_path) -> None:
    scene = {
        "scene_id": "scene_1",
        "local_bounds": [0, 0, 100, 100],
        "world_bounds": [0, 0, 100, 100],
        "local_to_world": {"origin": [0, 0], "scale": 1, "rotation_deg": 0},
        "primitives": [{
            "primitive_id": "p1",
            "command": "line",
            "points_local": [[10, 10], [90, 10]],
            "bbox_local": [10, 10, 90, 10],
            "bbox_world": [10, 10, 90, 10],
        }],
        "texts": [],
    }
    graph = {
        "objects": [{
            "object_id": "obj_1",
            "type": "wall",
            "subtype": "wall",
            "geometry_mode": "linear",
            "geometry_world": {"bbox": [10, 10, 90, 10], "polygon": []},
            "primitive_ids": ["p1"],
            "confidence": 0.9,
            "status": "confirmed",
            "semantic_source": "sympointv2",
            "evidence": {
                "sympoint_class": "wall",
                "visual_observations": [{"observation_id": "obs_1"}],
            },
        }],
        "validation": {"status": "ok", "issues": []},
    }
    sympoint_result = {
        "instances": [{"instance_id": "spv_1", "class_name": "wall", "score": 0.9, "primitive_ids": ["p1"]}],
        "semantic_by_primitive": [],
    }
    source_image = tmp_path / "scene.png"
    Image.new("RGB", (400, 300), "white").save(source_image)

    summary = generate_scene_comparison(
        scene,
        graph,
        source_image,
        tmp_path / "comparison",
        sympoint_result=sympoint_result,
        visual_observations=[{"object_id": "obj_1", "category": "wall"}],
    )

    source_layers = tmp_path / "comparison" / "source_layers.png"
    assert source_layers.is_file()
    assert (tmp_path / "comparison" / "source_sympointv2.png").is_file()
    assert (tmp_path / "comparison" / "source_visual_model.png").is_file()
    assert (tmp_path / "comparison" / "source_final_scene_graph.png").is_file()
    assert summary["source_layers"]["sympoint_prediction_count"] == 1
    assert summary["source_layers"]["visual_object_count"] == 1
    assert summary["source_layers"]["final_object_count"] == 1
    saved = json.loads((tmp_path / "comparison" / "comparison_summary.json").read_text(encoding="utf-8"))
    assert saved["files"]["source_layers"].endswith("source_layers.png")
    assert saved["files"]["source_visual_model"].endswith("source_visual_model.png")
