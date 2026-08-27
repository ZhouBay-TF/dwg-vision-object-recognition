from __future__ import annotations

import json

import dwg_vision.orchestrator as orchestrator_module
from dwg_vision.orchestrator import run_full_pipeline
from dwg_vision.providers import CallableVisionProvider


def test_full_pipeline_offline_contract_mode(tmp_path) -> None:
    dwg = tmp_path / "plan.dwg"
    dwg.write_bytes(b"fake-dwg-for-contract-test")
    raw = tmp_path / "dwg_raw.json"
    raw.write_text(json.dumps({
        "schema_version": "dwg_raw.v1",
        "source": {"filename": "plan.dwg"},
        "coordinate_system": {"world_bounds": [0, 0, 500, 400]},
        "frames": [{"scene_id": "scene_1", "world_bbox": [0, 0, 500, 400]}],
        "entities": [{
            "entity_id": "ent_1", "handle": "A1", "type": "wall", "subtype": "line",
            "bbox_world": [20, 20, 300, 40], "polygon_world": [[20, 20], [300, 20], [300, 40], [20, 40]],
        }],
        "annotations": [],
    }, ensure_ascii=False), encoding="utf-8")
    result = run_full_pipeline(
        dwg,
        tmp_path / "run",
        raw_json=raw,
        sympoint_mode="offline",
    )
    output = json.loads((tmp_path / "run" / "detection.json").read_text(encoding="utf-8"))
    assert result["output_json"].endswith("detection.json")
    assert output["schema_version"] == "detection.v1"
    assert output["scenes"][0]["objects"][0]["type"] == "wall"
    assert (tmp_path / "run" / "bundle" / "job_bundle.zip").is_file()


def test_full_pipeline_scene_overview_runs_before_downstream_review(tmp_path, monkeypatch) -> None:
    dwg = tmp_path / "plan.dwg"
    dwg.write_bytes(b"fake-dwg-for-vision-overview-test")
    raw = tmp_path / "dwg_raw.json"
    raw.write_text(json.dumps({
        "schema_version": "dwg_raw.v1",
        "source": {"filename": "plan.dwg"},
        "coordinate_system": {"world_bounds": [0, 0, 500, 400]},
        "frames": [{"scene_id": "scene_1", "world_bbox": [0, 0, 500, 400]}],
        "entities": [{
            "entity_id": "ent_1", "handle": "A1", "type": "wall", "subtype": "line",
            "bbox_world": [20, 20, 300, 40], "polygon_world": [[20, 20], [300, 20], [300, 40], [20, 40]],
        }],
        "annotations": [],
    }, ensure_ascii=False), encoding="utf-8")
    events: list[str] = []

    def vision_callback(path, prompt):
        events.append("scene_overview_or_roi")
        return {
            "scene_summary": "一个包含客厅的图框",
            "rooms_or_zones": [],
            "global_object_hypotheses": [],
            "text_observations": [],
            "review_regions": [],
            "global_warnings": [],
            "notes": "test",
            "category": "wall",
            "confidence": 0.8,
            "visible_facts": ["线性墙体"],
            "uncertainty": [],
            "contradictions": [],
        }

    original_offline_result = orchestrator_module._offline_sympoint_result

    def recorded_offline_result(scene):
        events.append("sympoint")
        return original_offline_result(scene)

    monkeypatch.setattr(
        orchestrator_module,
        "build_providers",
        lambda names: [CallableVisionProvider("test", "test-model", vision_callback)],
    )
    monkeypatch.setattr(orchestrator_module, "_offline_sympoint_result", recorded_offline_result)

    result = run_full_pipeline(
        dwg,
        tmp_path / "run_with_overview",
        raw_json=raw,
        sympoint_mode="offline",
        vision_provider_names=["test"],
        run_vision=True,
    )
    output = json.loads((tmp_path / "run_with_overview" / "detection.json").read_text(encoding="utf-8"))
    scene_trace = output["visual_review"]["scenes"]["scene_1"]
    assert scene_trace["scene_overview"]["status"] == "completed"
    assert scene_trace["scene_overview"]["budget"]["used_attempts"] == 1
    assert output["scenes"][0]["scene_overview"]["schema_version"] == "visual_scene_overview.v1"
    assert (tmp_path / "run_with_overview" / "scenes" / "scene_1" / "scene_overview.json").is_file()
    assert output["visual_review"]["scenes"]["scene_1"]["candidate_review"]["scene_overview_used"] is True
    assert events[0] == "scene_overview_or_roi"
    assert events.index("sympoint") > 0
    assert result["output_json"].endswith("detection.json")
