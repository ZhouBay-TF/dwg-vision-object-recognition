from __future__ import annotations

import json
import zipfile

from dwg_vision.contracts import SCHEMA_VERSIONS
from dwg_vision.scene import build_scene, build_scene_bundle, detect_frames, scene_to_svg


def _raw() -> dict:
    return {
        "schema_version": SCHEMA_VERSIONS["raw"],
        "source": {"filename": "plan.dwg", "sha256": "abc"},
        "coordinate_system": {"space": "world", "units": "mm", "world_bounds": [0, 0, 1000, 800]},
        "frames": [
            {"scene_id": "scene_a", "frame_type": "viewport", "world_bbox": [100, 100, 500, 500]},
            {"scene_id": "scene_b", "frame_type": "viewport", "world_bbox": [600, 100, 900, 500]},
        ],
        "entities": [
            {
                "entity_id": "ent_wall",
                "handle": "A01",
                "type": "wall",
                "subtype": "polyline",
                "bbox_world": [120, 120, 480, 150],
                "polygon_world": [[120, 120], [480, 120], [480, 150], [120, 150]],
                "layer": "A-WALL",
            },
            {
                "entity_id": "ent_block",
                "handle": "A03",
                "type": "furniture",
                "subtype": "FurnitureBlock",
                "entity_type": "AcDbBlockReference",
                "command": "block",
                "bbox_world": [220, 220, 280, 280],
                "points_world": [[250, 250]],
                "polygon_world": [],
                "layer": "A-FURN",
            },
            {
                "entity_id": "ent_toilet",
                "handle": "A02",
                "type": "furniture",
                "subtype": "toilet",
                "bbox_world": [700, 200, 760, 260],
                "polygon_world": [[700, 200], [760, 200], [760, 260], [700, 260]],
                "layer": "A-FURN",
            },
        ],
        "annotations": [
            {
                "text_id": "txt_1",
                "handle": "T01",
                "text": "坐便器",
                "bbox_world": [690, 180, 780, 200],
                "position_world": [735, 190],
                "layer": "A-TEXT",
            }
        ],
    }


def test_build_scene_keeps_world_and_local_identity() -> None:
    scene = build_scene(_raw(), detect_frames(_raw())[1])
    assert scene["scene_id"] == "scene_b"
    assert scene["local_to_world"]["origin"] == [600.0, 100.0]
    assert len(scene["primitives"]) == 1
    assert scene["primitives"][0]["source_entity_id"] == "ent_toilet"
    assert scene["primitives"][0]["bbox_local"] == [100.0, 100.0, 160.0, 160.0]
    assert scene["texts"][0]["role"] == "equipment_label"


def test_build_scene_bundle_has_remote_compatible_layout(tmp_path) -> None:
    bundle = build_scene_bundle(_raw(), tmp_path / "bundle", job_id="job_scene_test")
    assert bundle.is_file()
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        assert "job_bundle/job_manifest.json" in names
        assert "job_bundle/scenes/scene_a/scene.json" in names
        assert "job_bundle/scenes/scene_a/scene.svg" in names
        assert "job_bundle/scenes/scene_a/scene_s2.json" in names
        manifest = json.loads(archive.read("job_bundle/job_manifest.json"))
        assert manifest["schema_version"] == SCHEMA_VERSIONS["job"]
        assert {item["scene_id"] for item in manifest["scenes"]} == {"scene_a", "scene_b"}
        scene_a_manifest = next(item for item in manifest["scenes"] if item["scene_id"] == "scene_a")
        assert scene_a_manifest["preprocessor"] == "parse_svg_v5"
        scene_a_s2 = json.loads(archive.read("job_bundle/scenes/scene_a/scene_s2.json"))
        assert scene_a_s2["width"] == 400
        assert scene_a_s2["height"] == 400
        assert len(scene_a_s2["args"]) == scene_a_manifest["primitive_count"]
        assert set(scene_a_s2["semanticIds"]) == {35}


def test_block_reference_uses_bbox_proxy_for_sympoint_svg() -> None:
    scene = build_scene(_raw(), detect_frames(_raw())[0])
    block = next(item for item in scene["primitives"] if item["source_entity_id"] == "ent_block")
    assert len(block["points_local"]) == 4
    assert block["bbox_local"] == [120.0, 120.0, 180.0, 180.0]


def test_dimension_text_is_kept_as_evidence_but_not_rendered(tmp_path) -> None:
    raw = _raw()
    raw["annotations"].append({
        "text_id": "dim_1",
        "text": "18000.000000",
        "role": "dimension",
        "bbox_world": [100, 100, 900, 700],
        "position_world": [500, 400],
    })
    scene = build_scene(raw, detect_frames(raw)[0])
    assert any(item["role"] == "dimension" for item in scene["texts"])
    svg = scene_to_svg(scene, tmp_path / "scene.svg").read_text(encoding="utf-8")
    assert "18000.000000" not in svg
