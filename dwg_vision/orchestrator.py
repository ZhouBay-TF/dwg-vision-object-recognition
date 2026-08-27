from __future__ import annotations

"""The stable local DWG -> Scene Graph orchestration path."""

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .autocad_bridge import export_with_autocad
from .contracts import SCHEMA_VERSIONS
from .pipeline import _load_dotenv, build_providers
from .render import prepare_visual_source, render_svg_to_png
from .scene import build_scene_bundle
from .scene_graph import build_scene_graph
from .sympoint_client import SymPointRemoteClient
from .visual_review_agent import VisualReviewAgent
from .llm_fusion import DeepSeekFusionProvider, apply_fusion_decisions
from .comparison import generate_scene_comparison, write_run_report


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _offline_sympoint_result(scene: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic geometry-only result for local contract tests."""
    instances: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    class_ids = {"wall": 32, "window": 6, "door": 0, "furniture": 10}
    for index, primitive in enumerate(scene.get("primitives", [])):
        class_name = str(primitive.get("type") or "unknown")
        if class_name not in class_ids:
            continue
        primitive_id = str(primitive["primitive_id"])
        instances.append({
            "instance_id": f"offline_{scene['scene_id']}_{index:04d}",
            "class_id": class_ids[class_name],
            "class_name": class_name,
            "score": 0.45,
            "primitive_ids": [primitive_id],
        })
        semantic.append({
            "primitive_index": index,
            "primitive_id": primitive_id,
            "class_id": class_ids[class_name],
            "class_name": class_name,
            "score": 0.45,
        })
    return {
        "schema_version": SCHEMA_VERSIONS["sympoint"],
        "scene_id": scene["scene_id"],
        "input": {"primitive_count": len(scene.get("primitives", []))},
        "instances": instances,
        "semantic_by_primitive": semantic,
        "provenance": {"model": "offline-contract", "runtime_version": "local"},
    }


def _render_context(
    input_path: Path,
    raw: dict[str, Any],
    output_dir: Path,
    *,
    oda_exe: str | None,
    libredwg_exe: str | None,
    oda_version: str,
    layout: str,
    dpi: int,
    raster_max_dimension: int,
) -> tuple[Path | None, dict[str, Any], str | None]:
    """Prepare D3 rendering and a world-to-pixel manifest when possible."""
    try:
        render_path = prepare_visual_source(
            input_path,
            output_dir / "render",
            oda_exe=oda_exe,
            libredwg_exe=libredwg_exe,
            oda_version=oda_version,
            dpi=dpi,
            layout_name=layout,
            raster_max_dimension=raster_max_dimension,
            auto_focus=False,
        )
        from PIL import Image

        with Image.open(render_path) as image:
            width, height = image.size
        bounds = (raw.get("coordinate_system") or {}).get("world_bounds") or raw.get("world_bounds")
        manifest = {"width_px": width, "height_px": height, "world_bounds": bounds}
        return render_path, manifest, None
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return None, {}, str(exc)


def _render_scene_context(
    scene_dir: Path,
    scene: dict[str, Any],
    output_dir: Path,
    *,
    max_dimension: int,
) -> tuple[Path | None, dict[str, Any], str | None]:
    """Rasterize normalized Scene geometry with a Scene-local transform."""
    try:
        from PIL import Image

        render_path = render_svg_to_png(
            scene_dir / "scene.svg",
            output_dir / "render" / "scenes" / f"{scene['scene_id']}.png",
            # Keep the raster within a conservative memory envelope.  The
            # vector Scene/Bundle remains lossless; this cap only affects ROI
            # materialization for the visual model.
            max_dimension=min(int(max_dimension), 8192),
            auto_focus=False,
        )
        with Image.open(render_path) as image:
            width, height = image.size
        world_bounds = [float(value) for value in scene["world_bounds"]]
        local_width = max(1e-9, float(scene["local_bounds"][2]))
        local_height = max(1e-9, float(scene["local_bounds"][3]))
        return render_path, {
            "width_px": width,
            "height_px": height,
            "world_bounds": world_bounds,
            "world_to_pixel": {
                "origin_world": [world_bounds[0], world_bounds[1]],
                "scale_x": width / local_width,
                "scale_y": height / local_height,
            },
            "renderer": "scene_svg_from_autocad_geometry",
        }, None
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
        return None, {}, str(exc)


def run_full_pipeline(
    input_path: Path,
    output_dir: Path,
    *,
    raw_json: Path | None = None,
    use_autocad: bool = False,
    autocad_prog_id: str | None = None,
    autocad_visible: bool = False,
    sympoint_url: str | None = None,
    sympoint_mode: str = "remote",
    vision_provider_names: list[str] | None = None,
    run_vision: bool = False,
    oda_exe: str | None = None,
    libredwg_exe: str | None = None,
    oda_version: str = "ACAD2018",
    layout: str = "Model",
    dpi: int = 300,
    raster_max_dimension: int = 16000,
    job_id: str | None = None,
    visual_max_attempts_per_object: int = 3,
    visual_max_attempts_per_scene: int = 30,
    fusion_provider_name: str = "",
) -> dict[str, Any]:
    """Run the complete local orchestration path.

    ``sympoint_mode=offline`` is a contract-test fallback and must not be
    mistaken for model inference.  Production runs use ``remote`` through an
    SSH/VSCode local port forward.
    """
    _load_dotenv()
    input_path = Path(input_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_job_id = job_id or f"job_{uuid.uuid4().hex[:16]}"

    raw_path = Path(raw_json) if raw_json else output_dir / "dwg_raw.json"
    extraction_error: str | None = None
    if raw_json:
        raw = _load_json(raw_path)
    elif use_autocad:
        try:
            export_with_autocad(
                input_path,
                raw_path,
                prog_id=autocad_prog_id,
                visible=autocad_visible,
            )
            raw = _load_json(raw_path)
        except (RuntimeError, FileNotFoundError, ValueError) as exc:
            raise RuntimeError(f"AutoCAD 原生解析失败：{exc}") from exc
    else:
        raise RuntimeError("DWG 主流程需要 --autocad，或使用 --raw-json 提供 AutoCAD 导出的 dwg_raw JSON")

    bundle_dir = output_dir / "bundle"
    bundle_path = build_scene_bundle(raw, bundle_dir, source_path=input_path, job_id=selected_job_id)
    manifest = _load_json(bundle_dir / "job_manifest.json")

    # Render every Scene before model calls.  The Scene image is already
    # cropped to one drawing frame, so the first vision call can establish a
    # stable global prior for that frame before SymPointV2 and ROI review.
    render_path, render_manifest, render_error = _render_context(
        input_path,
        raw,
        output_dir,
        oda_exe=oda_exe,
        libredwg_exe=libredwg_exe,
        oda_version=oda_version,
        layout=layout,
        dpi=dpi,
        raster_max_dimension=raster_max_dimension,
    )
    scene_render_contexts: dict[str, dict[str, Any]] = {}
    for scene_item in manifest["scenes"]:
        scene_dir = bundle_dir / scene_item["path"]
        scene = _load_json(scene_dir / "scene.json")
        scene_render_path, scene_render_manifest, scene_render_error = _render_scene_context(
            scene_dir,
            scene,
            output_dir,
            max_dimension=raster_max_dimension,
        )
        scene_render_contexts[scene["scene_id"]] = {
            "path": scene_render_path,
            "manifest": scene_render_manifest,
            "error": scene_render_error,
        }

    vision_trace: dict[str, Any] = {"status": "disabled", "scenes": {}}
    provider = None
    visual_agent: VisualReviewAgent | None = None
    if run_vision and vision_provider_names:
        try:
            providers = build_providers(vision_provider_names)
            provider = providers[0]
            visual_agent = VisualReviewAgent(
                provider,
                max_attempts_per_object=visual_max_attempts_per_object,
                max_attempts_per_scene=visual_max_attempts_per_scene,
            )
            vision_trace = {"status": "enabled", "provider": getattr(provider, "name", "unknown"), "scenes": {}}
        except RuntimeError as exc:
            vision_trace = {"status": "failed_provider_setup", "error": str(exc), "scenes": {}}
    elif run_vision:
        vision_trace = {"status": "skipped_no_provider", "scenes": {}}

    # Exactly one full-Scene visual call per frame, before SymPointV2.  The
    # result is context/evidence only; it cannot create objects or mutate CAD
    # geometry and native text remains authoritative downstream.
    scene_overviews: dict[str, dict[str, Any] | None] = {}
    if visual_agent is not None:
        for scene_item in manifest["scenes"]:
            scene_dir = bundle_dir / scene_item["path"]
            scene = _load_json(scene_dir / "scene.json")
            overview_trace = visual_agent.overview(
                scene,
                render_path=scene_render_contexts.get(scene["scene_id"], {}).get("path"),
                output_dir=output_dir / "visual_review",
                texts=scene.get("texts", []),
            )
            scene_overviews[scene["scene_id"]] = overview_trace.get("overview")
            vision_trace["scenes"][scene["scene_id"]] = {"scene_overview": overview_trace}

    remote_metadata: dict[str, Any] = {"mode": sympoint_mode, "status": "not_run"}
    scene_results: dict[str, dict[str, Any]] = {}
    if sympoint_mode == "remote":
        client = SymPointRemoteClient(sympoint_url or os.getenv("SYMPOINT_BASE_URL", "http://127.0.0.1:8000"))
        try:
            remote_metadata["health"] = client.health()
            remote_metadata["ready"] = client.ready()
            remote = client.infer_bundle(bundle_path, output_dir / "sympoint")
            remote_metadata.update({"status": "completed", "job_id": remote["job_id"], "remote_status": remote["status"]})
            scene_results = dict(remote["result"]["scenes"])
            expected_scene_ids = {str(item["scene_id"]) for item in manifest["scenes"]}
            missing_scene_ids = sorted(expected_scene_ids - set(scene_results))
            if missing_scene_ids:
                raise RuntimeError(f"SymPointV2 结果缺少 Scene：{', '.join(missing_scene_ids)}")
        except Exception as exc:  # keep raw/Bundle artifacts for retry and audit
            remote_metadata.update({
                "status": "degraded_local_fallback",
                "error": str(exc),
                "fallback": "native_classified_geometry_only",
            })
            for scene_item in manifest["scenes"]:
                scene = _load_json(bundle_dir / scene_item["path"] / "scene.json")
                scene_results[scene["scene_id"]] = _offline_sympoint_result(scene)
    elif sympoint_mode == "offline":
        remote_metadata["status"] = "offline_contract_mode"
        for scene_item in manifest["scenes"]:
            scene = _load_json(bundle_dir / scene_item["path"] / "scene.json")
            scene_results[scene["scene_id"]] = _offline_sympoint_result(scene)
    else:
        raise ValueError("sympoint_mode must be 'remote' or 'offline'")

    fusion_provider = None
    fusion_metadata: dict[str, Any] = {"status": "disabled"}
    if fusion_provider_name:
        if fusion_provider_name.lower() != "deepseek":
            raise ValueError("fusion_provider_name currently supports only 'deepseek'")
        try:
            fusion_provider = DeepSeekFusionProvider()
            fusion_metadata = {
                "status": "enabled",
                "provider": fusion_provider.name,
                "model": fusion_provider.model,
            }
        except RuntimeError as exc:
            fusion_metadata = {"status": "failed_provider_setup", "error": str(exc)}

    graphs: list[dict[str, Any]] = []
    comparison_artifacts: dict[str, dict[str, Any]] = {}
    for scene_item in manifest["scenes"]:
        scene_path = bundle_dir / scene_item["path"] / "scene.json"
        scene = _load_json(scene_path)
        sympoint_result = scene_results.get(scene["scene_id"])
        if sympoint_result is None:
            raise RuntimeError(f"SymPointV2 result missing for Scene {scene['scene_id']}")
        scene_overview = scene_overviews.get(scene["scene_id"])
        graph_seed = build_scene_graph(
            scene,
            sympoint_result,
            scene_overview=scene_overview,
            job_id=selected_job_id,
        )
        scene_trace: dict[str, Any] | None = None
        if visual_agent is not None:
            scene_trace = visual_agent.review(
                scene,
                graph_seed["objects"],
                render_path=scene_render_contexts.get(scene["scene_id"], {}).get("path"),
                render_manifest=scene_render_contexts.get(scene["scene_id"], {}).get("manifest", {}),
                output_dir=output_dir / "visual_review",
                texts=scene.get("texts", []),
                scene_overview=scene_overview,
            )
            graph = build_scene_graph(
                scene,
                sympoint_result,
                observations=scene_trace["observations"],
                texts=scene.get("texts", []),
                scene_overview=scene_overview,
                job_id=selected_job_id,
            )
        else:
            graph = graph_seed
        if fusion_provider is not None:
            try:
                fusion_response = fusion_provider.fuse({
                    "scene_id": scene["scene_id"],
                    "objects": graph.get("objects", []),
                    "texts": scene.get("texts", []),
                    "scene_overview": graph.get("scene_overview"),
                    "visual_observations": (scene_trace or {}).get("observations", []),
                })
                graph = apply_fusion_decisions(
                    graph,
                    fusion_response,
                    provider=fusion_provider.name,
                    model=fusion_provider.model,
                )
            except Exception as exc:  # optional enhancement must not erase baseline output
                fusion_metadata["status"] = "failed"
                fusion_metadata.setdefault("errors", []).append({"scene_id": scene["scene_id"], "error": str(exc)})
        scene_output = output_dir / "scenes" / scene["scene_id"]
        scene_output.mkdir(parents=True, exist_ok=True)
        (scene_output / "scene_graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        (scene_output / "sympoint_result.json").write_text(json.dumps(sympoint_result, ensure_ascii=False, indent=2), encoding="utf-8")
        if scene_overview is not None:
            (scene_output / "scene_overview.json").write_text(json.dumps(scene_overview, ensure_ascii=False, indent=2), encoding="utf-8")
        if scene_trace is not None:
            (scene_output / "visual_review_trace.json").write_text(json.dumps(scene_trace, ensure_ascii=False, indent=2), encoding="utf-8")
            vision_trace["scenes"].setdefault(scene["scene_id"], {})["candidate_review"] = scene_trace
        try:
            comparison_artifacts[scene["scene_id"]] = generate_scene_comparison(
                scene,
                graph,
                scene_render_contexts.get(scene["scene_id"], {}).get("path"),
                output_dir / "comparison" / scene["scene_id"],
            )
        except (FileNotFoundError, ImportError, OSError, RuntimeError, ValueError) as exc:
            # Comparison is an audit aid; it must not erase the machine-readable
            # detection result when an optional raster dependency is unavailable.
            comparison_artifacts[scene["scene_id"]] = {
                "status": "failed",
                "scene_id": scene["scene_id"],
                "error": str(exc),
            }
        graphs.append(graph)

    validation = {
        "status": "ok" if all(graph["validation"]["status"] == "ok" for graph in graphs) else "warning",
        "scene_count": len(graphs),
        "issues": [
            {"scene_id": graph["scene_id"], **issue}
            for graph in graphs
            for issue in graph["validation"].get("issues", [])
        ],
    }
    result = {
        "schema_version": "detection.v1",
        "job_id": selected_job_id,
        "source": {
            "path": str(input_path),
            "format": input_path.suffix.lower().lstrip("."),
            "raw_json": str(raw_path),
        },
        "coordinate_space": "world",
        "bundle": {"path": str(bundle_path), "manifest": manifest},
        "remote_sympoint": remote_metadata,
        "render": {
            "path": str(render_path) if render_path else None,
            "manifest": render_manifest,
            "error": render_error,
            "scene_renders": {
                scene_id: {
                    "path": str(value["path"]) if value.get("path") else None,
                    "manifest": value.get("manifest", {}),
                    "error": value.get("error"),
                }
                for scene_id, value in scene_render_contexts.items()
            },
        },
        "visual_review": vision_trace,
        "llm_fusion": fusion_metadata,
        "scenes": graphs,
        "validation": validation,
        "artifacts": {
            "bundle_dir": str(bundle_dir),
            "scene_dir": str(output_dir / "scenes"),
            "visual_review_dir": str(output_dir / "visual_review"),
            "comparison_dir": str(output_dir / "comparison"),
            "comparisons": comparison_artifacts,
        },
    }
    result["artifacts"]["run_report"] = str(output_dir / "run_report.json")
    write_run_report(output_dir / "run_report.json", result, raw, comparison_artifacts)
    output_json = output_dir / "detection.json"
    result["output_json"] = str(output_json)
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
