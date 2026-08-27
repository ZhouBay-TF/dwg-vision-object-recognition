from __future__ import annotations

import json

from PIL import Image, ImageDraw

from dwg_vision.pipeline import detect_drawing
from dwg_vision.tiling import create_tile_manifest


def _floorplan(path) -> None:
    image = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 150, 210), outline="black", width=4)
    draw.rectangle((170, 30, 300, 180), outline="black", width=4)
    draw.line((150, 120, 190, 120), fill="black", width=4)
    image.save(path)


def test_manifest_contains_reversible_mosaics(tmp_path) -> None:
    source = tmp_path / "plan.png"
    _floorplan(source)
    manifest_path = create_tile_manifest(
        source,
        tmp_path / "artifacts",
        tile_size=128,
        overlap=32,
        overview_max=256,
        mosaic_group_size=4,
        mosaic_max_dimension=256,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    page = manifest["pages"][0]
    assert page["mosaics"]
    assert len(page["mosaics"][0]["panels"]) <= 4
    assert (tmp_path / "artifacts" / page["mosaics"][0]["path"]).is_file()
    assert (tmp_path / "artifacts" / "preprocess" / "plan" / "ink_mask.png").is_file()


def test_offline_pipeline_writes_full_json_contract(tmp_path) -> None:
    source = tmp_path / "plan.png"
    _floorplan(source)
    result = detect_drawing(
        source,
        tmp_path / "run",
        provider_names=["offline"],
        tile_size=128,
        overlap=32,
        overview_max=256,
        mosaic_group_size=4,
        mosaic_max_dimension=256,
    )
    output = json.loads((tmp_path / "run" / "detection.json").read_text(encoding="utf-8"))
    assert output["schema_version"] == "1.0"
    assert output["summary"]["processed_mosaics"] > 0
    assert output["model_runs"][0]["provider"] == "offline"
    assert result["output_json"].endswith("detection.json")


def test_geometry_sidecar_is_fused_into_pixel_output(tmp_path) -> None:
    source = tmp_path / "plan.png"
    _floorplan(source)
    sidecar = tmp_path / "geometry.json"
    sidecar.write_text(
        json.dumps(
            {
                "coordinate_space": "world",
                "coordinate_system": {"space": "world", "units": "mm", "world_bounds": [0, 0, 320, 240]},
                "entities": [
                    {
                        "type": "wall",
                        "subtype": "A-WALL",
                        "bbox_px": [0, 0, 100, 20],
                        "confidence": 0.9,
                        "evidence": {"method": "autocad"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = detect_drawing(
        source,
        tmp_path / "run_sidecar",
        provider_names=["offline"],
        geometry_json=sidecar,
        tile_size=128,
        overlap=32,
        overview_max=256,
        mosaic_group_size=4,
        mosaic_max_dimension=256,
    )
    assert result["summary"]["wall_count"] == 1
    assert result["entities"][0]["coordinate_space"] == "world+pixel"
    assert result["entities"][0]["bbox_world"] == [0.0, 0.0, 100.0, 20.0]
