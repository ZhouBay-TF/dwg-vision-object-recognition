from __future__ import annotations

"""End-to-end whole-drawing recognition pipeline."""

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .geometry import extract_vector_evidence
from .providers import ArkVisionProvider, DeepSeekVisionProvider, OfflineVisionProvider, VisionProvider, build_detection_prompt
from .reconcile import collect_visual_candidates, reconcile_candidates
from .render import prepare_visual_source
from .schema import empty_detection_document
from .tiling import create_tile_manifest
from .validation import validate_entities


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        return


def _provider(name: str) -> VisionProvider:
    normalized = name.strip().lower()
    if normalized in {"deepseek", "ds"}:
        return DeepSeekVisionProvider()
    if normalized in {"ark", "doubao", "volcengine"}:
        return ArkVisionProvider()
    if normalized in {"offline", "none", "mock"}:
        return OfflineVisionProvider()
    raise ValueError(f"未知视觉提供商：{name}")


def build_providers(names: Iterable[str]) -> list[VisionProvider]:
    providers: list[VisionProvider] = []
    errors: list[str] = []
    for name in names:
        try:
            providers.append(_provider(name))
        except RuntimeError as exc:
            errors.append(f"{name}: {exc}")
    if not providers:
        raise RuntimeError("没有可用的视觉提供商。配置 DEEPSEEK_API_KEY/ARK_API_KEY，或使用 --providers offline。\n" + "\n".join(errors))
    return providers


def _world_bounds(vector_bounds: dict[str, Any], entities: list[dict[str, Any]]) -> list[float] | None:
    raw = vector_bounds.get("world_bounds")
    if raw and len(raw) == 4:
        return [float(value) for value in raw]
    boxes = [entity.get("bbox_px") for entity in entities if len(entity.get("bbox_px", [])) == 4]
    if not boxes:
        return None
    return [
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    ]


def _world_to_pixel_box(box: list[float], bounds: list[float], page: dict[str, Any]) -> list[float]:
    min_x, min_y, max_x, max_y = bounds
    width, height = float(page["width"]), float(page["height"])
    span_x, span_y = max(1e-9, max_x - min_x), max(1e-9, max_y - min_y)
    return [
        (box[0] - min_x) / span_x * width,
        (max_y - box[3]) / span_y * height,
        (box[2] - min_x) / span_x * width,
        (max_y - box[1]) / span_y * height,
    ]


def _world_to_pixel_annotations(annotations: list[dict[str, Any]], bounds: list[float] | None, page: dict[str, Any]) -> list[dict[str, Any]]:
    if not bounds:
        return annotations
    result: list[dict[str, Any]] = []
    for annotation in annotations:
        copied = dict(annotation)
        if annotation.get("bbox_world"):
            copied["bbox_px"] = _world_to_pixel_box([float(value) for value in annotation["bbox_world"]], bounds, page)
        result.append(copied)
    return result


def _vector_to_pixel(entities: list[dict[str, Any]], bounds: list[float] | None, page: dict[str, Any]) -> list[dict[str, Any]]:
    if not bounds:
        return []
    result: list[dict[str, Any]] = []
    for entity in entities:
        world_box = [float(value) for value in entity.get("bbox_px", [0, 0, 0, 0])]
        copied = dict(entity)
        copied["bbox_world"] = world_box
        copied["bbox_px"] = _world_to_pixel_box(world_box, bounds, page)
        copied["coordinate_space"] = "world+pixel"
        if copied.get("polygon_px"):
            copied["polygon_world"] = copied["polygon_px"]
            polygon = copied["polygon_px"]
            copied["polygon_px"] = [
                list(_world_to_pixel_box([point[0], point[1], point[0], point[1]], bounds, page)[:2])
                for point in polygon
            ]
        copied["evidence"] = {
            **copied.get("evidence", {}),
            "vector_bbox_world": world_box,
            "geometry_source": copied.get("provenance", {}).get("source", "vector"),
        }
        copied["provenance"] = {**copied.get("provenance", {}), "provider": "vector"}
        result.append(copied)
    return result


def _assign_ids(entities: list[dict[str, Any]]) -> None:
    counters: dict[str, int] = {}
    for entity in entities:
        entity_type = entity["type"]
        counters[entity_type] = counters.get(entity_type, 0) + 1
        entity["id"] = f"{entity_type}_{counters[entity_type]:04d}"


def detect_drawing(
    input_path: Path,
    output_dir: Path,
    *,
    provider_names: Iterable[str] = ("ark", "deepseek"),
    geometry_json: Path | None = None,
    use_autocad: bool = False,
    autocad_prog_id: str | None = None,
    autocad_visible: bool = False,
    oda_exe: str | None = None,
    libredwg_exe: str | None = None,
    oda_version: str = "ACAD2018",
    layout_name: str = "Model",
    dpi: int = 300,
    svg_max_dimension: int = 16000,
    raster_max_dimension: int = 16000,
    tile_size: int = 3072,
    overlap: int = 384,
    overview_max: int = 6000,
    mosaic_group_size: int = 4,
    mosaic_max_dimension: int = 8192,
    limit_mosaics: int | None = None,
) -> dict[str, Any]:
    """Run the full vector + raster + multi-model + validation workflow."""
    _load_dotenv()
    input_path = input_path.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    native_geometry_json = geometry_json
    if use_autocad:
        from .autocad_bridge import export_with_autocad

        native_geometry_json = export_with_autocad(
            input_path,
            output_dir / "autocad_geometry.json",
            prog_id=autocad_prog_id,
            visible=autocad_visible,
        )

    visual_source = prepare_visual_source(
        input_path,
        output_dir,
        oda_exe=oda_exe,
        libredwg_exe=libredwg_exe,
        oda_version=oda_version,
        dpi=dpi,
        layout_name=layout_name,
        svg_max_dimension=svg_max_dimension,
        raster_max_dimension=raster_max_dimension,
        auto_focus=False,
    )
    manifest_path = create_tile_manifest(
        visual_source,
        output_dir,
        tile_size=tile_size,
        overlap=overlap,
        overview_max=overview_max,
        dpi=dpi,
        mosaic_group_size=mosaic_group_size,
        mosaic_max_dimension=mosaic_max_dimension,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    vector_entities, annotations, vector_system = extract_vector_evidence(input_path, geometry_json=native_geometry_json)
    result = empty_detection_document(source=str(input_path))
    result["source"] = {
        "path": str(input_path),
        "format": input_path.suffix.lower().lstrip("."),
        "visual_source": str(visual_source),
        "manifest": str(manifest_path),
    }
    result["coordinate_system"] = {
        "space": "pixel_with_optional_world_evidence",
        "units": vector_system.get("units", "unknown"),
        "origin": "top_left_for_pixel; bottom_left_for_world",
        "world_bounds": None,
    }

    providers = build_providers(provider_names)
    visual_candidates: list[dict[str, Any]] = []
    model_runs: list[dict[str, Any]] = []
    processed_mosaics = 0
    for page in manifest.get("pages", []):
        mosaics = page.get("mosaics", [])
        for mosaic in mosaics:
            if limit_mosaics is not None and processed_mosaics >= limit_mosaics:
                break
            mosaic_path = output_dir / mosaic["path"]
            prompt = build_detection_prompt(mosaic)
            for provider in providers:
                run_record: dict[str, Any] = {
                    "provider": provider.name,
                    "model": provider.model,
                    "mosaic_id": mosaic["mosaic_id"],
                    "status": "started",
                }
                try:
                    response = provider.analyze(mosaic_path, prompt)
                    candidates = collect_visual_candidates(
                        response=response,
                        mosaic=mosaic,
                        page=page,
                        provider=provider.name,
                        model=provider.model,
                    )
                    visual_candidates.extend(candidates)
                    run_record.update(
                        {
                            "status": "ok",
                            "candidate_count": len(candidates),
                            "notes": str(response.get("notes") or ""),
                        }
                    )
                except Exception as exc:  # provider failures remain auditable and do not hide vector evidence
                    run_record.update({"status": "error", "error": str(exc)})
                model_runs.append(run_record)
            processed_mosaics += 1
        if limit_mosaics is not None and processed_mosaics >= limit_mosaics:
            break

    page_for_coordinates = manifest.get("pages", [{}])[0]
    world_bounds = _world_bounds(vector_system, vector_entities)
    result["coordinate_system"]["world_bounds"] = world_bounds
    vector_pixel_entities = _vector_to_pixel(vector_entities, world_bounds, page_for_coordinates)
    all_candidates = reconcile_candidates(visual_candidates + vector_pixel_entities)
    _assign_ids(all_candidates)
    pixel_annotations = _world_to_pixel_annotations(annotations, world_bounds, page_for_coordinates)
    disagreement_notes = [
        f"{run['provider']} 在 {run['mosaic_id']} 调用失败：{run['error']}"
        for run in model_runs
        if run.get("status") == "error"
    ]
    validation = validate_entities(
        all_candidates,
        annotations=pixel_annotations,
        page_width=page_for_coordinates.get("width"),
        page_height=page_for_coordinates.get("height"),
        disagreement_notes=disagreement_notes,
    )

    result["entities"] = all_candidates
    result["annotations"] = pixel_annotations
    result["model_runs"] = model_runs
    result["validation"] = validation
    result["artifacts"] = {
        "manifest": str(manifest_path),
        "overview": [str(output_dir / page["overview_path"]) for page in manifest.get("pages", [])],
        "mosaics": [str(output_dir / mosaic["path"]) for page in manifest.get("pages", []) for mosaic in page.get("mosaics", [])],
        "preprocess": str(output_dir / "preprocess"),
        "vector_evidence": str(native_geometry_json) if native_geometry_json else None,
    }
    result["summary"] = {
        "processed_mosaics": processed_mosaics,
        "wall_count": sum(entity["type"] == "wall" for entity in all_candidates),
        "window_count": sum(entity["type"] == "window" for entity in all_candidates),
        "door_count": sum(entity["type"] == "door" for entity in all_candidates),
        "furniture_count": sum(entity["type"] == "furniture" for entity in all_candidates),
        "model_observations": len(visual_candidates),
        "vector_observations": len(vector_pixel_entities),
    }
    output_json = output_dir / "detection.json"
    result["output_json"] = str(output_json)
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
