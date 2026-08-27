from __future__ import annotations

from dwg_vision.sympoint_audit import audit_sympoint_input, audit_sympoint_output


def _scene() -> dict:
    return {
        "schema_version": "dwg_scene.v1",
        "scene_id": "scene_a",
        "local_bounds": [0, 0, 1000, 1000],
        "world_bounds": [0, 0, 1000, 1000],
        "primitives": [
            {
                "primitive_id": "p0",
                "points_local": [[100, 100], [900, 100]],
                "bbox_local": [100, 100, 900, 100],
                "entity_type": "AcDbLine",
                "type": "wall",
                "layer": "WALL",
            },
            {
                "primitive_id": "p1",
                "points_local": [[200, 200], [300, 200]],
                "bbox_local": [200, 200, 300, 200],
                "entity_type": "AcDbBlockReference",
                "type": "window",
                "layer": "AD-AXIS-DIMS",
            },
        ],
    }


def test_input_audit_reports_distribution_gaps_without_rejecting_payload() -> None:
    scene = _scene()
    s2 = {
        "width": 1000,
        "height": 1000,
        "commands": [0, 0],
        "args": [[100, 100, 900, 100, 900, 100, 100, 100], [200, 200, 300, 200, 300, 200, 200, 200]],
        "lengths": [800, 100],
        "layerIds": [1, 2],
        "widths": [1, 1],
    }
    audit = audit_sympoint_input(scene, s2)
    codes = {item["code"] for item in audit["warnings"]}
    assert audit["status"] == "review_required"
    assert audit["padded_count"] == 2048
    assert "block_reference_proxy" in codes
    assert "auxiliary_layers_present" in codes


def test_input_audit_flags_large_all_line_scene() -> None:
    scene = _scene()
    scene["primitives"] = [
        {**scene["primitives"][0], "primitive_id": f"p{i}"}
        for i in range(100)
    ]
    s2 = {
        "width": 1000,
        "height": 1000,
        "commands": [0] * 100,
        "args": [[100, 100, 900, 100, 900, 100, 100, 100]] * 100,
        "lengths": [800] * 100,
        "layerIds": [1] * 100,
        "widths": [1] * 100,
    }
    audit = audit_sympoint_input(scene, s2)
    assert "all_line_primitives" in {item["code"] for item in audit["warnings"]}


def test_output_audit_keeps_missing_native_thing_unresolved() -> None:
    scene = _scene()
    result = {
        "scene_id": "scene_a",
        "input": {"primitive_count": 2},
        "instances": [{"class_name": "wall", "score": 0.9}],
        "semantic_by_primitive": [{"score": 0.04}, {"score": 0.05}],
    }
    audit = audit_sympoint_output(scene, result)
    assert audit["status"] == "review_required"
    assert audit["missing_expected_thing_classes"] == ["window"]
    assert any(item["code"] == "expected_thing_not_detected" for item in audit["warnings"])


def test_output_audit_checks_primitive_alignment() -> None:
    scene = _scene()
    result = {"scene_id": "scene_a", "input": {"primitive_count": 3}, "instances": []}
    audit = audit_sympoint_output(scene, result)
    assert audit["status"] == "error"
    assert any(item["code"] == "output_primitive_count_mismatch" for item in audit["warnings"])
