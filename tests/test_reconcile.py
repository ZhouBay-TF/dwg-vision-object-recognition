from __future__ import annotations

from dwg_vision.reconcile import collect_visual_candidates, reconcile_candidates
from dwg_vision.validation import validate_entities


def _mosaic() -> dict:
    return {
        "mosaic_id": "page_m000",
        "width": 400,
        "height": 200,
        "panels": [
            {
                "panel_id": "tile_a",
                "x": 0,
                "y": 0,
                "width": 200,
                "height": 200,
                "scale": 1.0,
                "global_x": 100,
                "global_y": 20,
            }
        ],
    }


def test_panel_boxes_are_mapped_and_duplicate_models_are_reconciled() -> None:
    page = {"width": 800, "height": 600}
    response = {
        "detections": [
            {
                "panel_id": "tile_a",
                "type": "door",
                "subtype": "平开门",
                "bbox_px": [20, 40, 80, 100],
                "polygon_px": [[20, 40], [80, 40], [80, 100], [20, 100]],
                "rotation_deg": 0,
                "dimensions": {"width_px": 60},
                "confidence": 0.80,
                "evidence": "可见门扇和开启弧线",
                "nearby_text": [],
            }
        ],
        "notes": "",
    }
    first = collect_visual_candidates(response=response, mosaic=_mosaic(), page=page, provider="ark", model="vision-a")
    second = collect_visual_candidates(response=response, mosaic=_mosaic(), page=page, provider="deepseek", model="vision-b")
    entities = reconcile_candidates(first + second)
    assert len(entities) == 1
    assert entities[0]["bbox_px"] == [120.0, 60.0, 180.0, 120.0]
    assert entities[0]["evidence"]["model_support"] == 2
    assert set(entities[0]["evidence"]["providers"]) == {"ark", "deepseek"}
    assert entities[0]["polygon_px"][0] == [120.0, 60.0]


def test_nearby_label_mismatch_is_reported() -> None:
    entities = [
        {
            "id": "door_0001",
            "type": "door",
            "subtype": "unknown",
            "bbox_px": [100, 100, 180, 180],
            "confidence": 0.8,
            "evidence": {},
        }
    ]
    report = validate_entities(
        entities,
        annotations=[{"text": "窗", "bbox_px": [120, 120, 140, 140]}],
        page_width=500,
        page_height=500,
    )
    assert report["status"] == "error"
    assert any(issue["code"] == "nearby_label_mismatch" for issue in report["issues"])
