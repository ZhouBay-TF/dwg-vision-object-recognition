from __future__ import annotations

import argparse
import json
from pathlib import Path

from .render import prepare_visual_source
from .tiling import create_tile_manifest
from .vision import analyze_manifest
from .pipeline import detect_drawing
from .orchestrator import run_full_pipeline


def _add_prepare_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, type=Path, help="DWG/DXF/PDF/PNG 输入文件")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--oda-exe", help="ODAFileConverter.exe 路径，仅 DWG 输入需要")
    parser.add_argument("--libredwg-exe", help="LibreDWG dwg2SVG.exe 路径，仅 DWG 输入需要")
    parser.add_argument("--oda-version", default="ACAD2018")
    parser.add_argument("--layout", default="Model")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--svg-max-dimension", type=int, default=16000)
    parser.add_argument("--raster-max-dimension", type=int, default=16000)
    parser.add_argument("--tile-size", type=int, default=3072)
    parser.add_argument("--overlap", type=int, default=384)
    parser.add_argument("--overview-max", type=int, default=6000)


def _add_detect_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, type=Path, help="DWG/DXF/PDF/PNG 输入文件")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--providers",
        default="ark,deepseek",
        help="视觉提供商，逗号分隔：ark,deepseek 或 offline；默认先 Ark 后 DeepSeek",
    )
    parser.add_argument("--geometry-json", type=Path, help="AutoCAD/其他 CAD 导出的几何证据 JSON")
    parser.add_argument("--autocad", action="store_true", help="使用本机 AutoCAD 2020-2027 ActiveX 导出几何证据")
    parser.add_argument("--autocad-prog-id", help="覆盖 AutoCAD COM ProgID；默认按 2027 到 2020 自动探测")
    parser.add_argument("--autocad-visible", action="store_true")
    parser.add_argument("--oda-exe", help="ODAFileConverter.exe 路径")
    parser.add_argument("--libredwg-exe", help="LibreDWG dwg2SVG.exe 路径")
    parser.add_argument("--oda-version", default="ACAD2018")
    parser.add_argument("--layout", default="Model")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--svg-max-dimension", type=int, default=16000)
    parser.add_argument("--raster-max-dimension", type=int, default=16000)
    parser.add_argument("--tile-size", type=int, default=3072)
    parser.add_argument("--overlap", type=int, default=384)
    parser.add_argument("--overview-max", type=int, default=6000)
    parser.add_argument("--mosaic-group-size", type=int, default=4)
    parser.add_argument("--mosaic-max-dimension", type=int, default=8192)
    parser.add_argument("--limit-mosaics", type=int)


def _add_full_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, type=Path, help="DWG 输入文件")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--raw-json", type=Path, help="已有的 AutoCAD dwg_raw JSON；提供后可跳过 COM 解析")
    parser.add_argument("--autocad", action="store_true", help="使用本机 AutoCAD 2020-2027 ActiveX 导出原生数据")
    parser.add_argument("--autocad-prog-id", help="覆盖 AutoCAD COM ProgID")
    parser.add_argument("--autocad-visible", action="store_true")
    parser.add_argument("--sympoint-url", default=None, help="SymPointV2 API 地址，默认读取 SYMPOINT_BASE_URL")
    parser.add_argument("--sympoint-mode", choices=["remote", "offline"], default="remote")
    parser.add_argument("--vision-providers", default="", help="视觉复核提供商，当前使用第一个：ark 或 deepseek")
    parser.add_argument(
        "--run-vision",
        action="store_true",
        help="启用视觉阶段：每个 Scene 先做 1 次全图概览，再运行有界 ROI 复核",
    )
    parser.add_argument("--fusion-provider", choices=["", "deepseek"], default="", help="Scene Graph 内部的可选 LLM 融合器")
    parser.add_argument("--oda-exe", help="ODAFileConverter.exe 路径，用于生成高清渲染")
    parser.add_argument("--libredwg-exe", help="LibreDWG dwg2SVG.exe 路径，用于生成高清渲染")
    parser.add_argument("--oda-version", default="ACAD2018")
    parser.add_argument("--layout", default="Model")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--raster-max-dimension", type=int, default=16000)
    parser.add_argument("--job-id")
    parser.add_argument("--sympoint-tile-size", type=float, help="按 CAD 世界坐标切分为 SYP 局部识别块")
    parser.add_argument("--sympoint-tile-overlap", type=float, default=0.0, help="SYP 分块重叠宽度")
    parser.add_argument("--sympoint-tile-min-primitives", type=int, default=0, help="忽略图元过少的空白 SYP 分块")
    parser.add_argument("--skip-comparison", action="store_true", help="跳过批量 PNG 对比图，仅输出检测 JSON")
    parser.add_argument("--skip-visual-overview", action="store_true", help="跳过全 Scene 视觉概览，仅复核 SYP 候选")
    parser.add_argument("--legacy-candidate-mode", action="store_true", help="恢复原生 CAD 候选发现与闭合区域候选链")
    parser.add_argument("--visual-max-attempts-per-object", type=int, default=3)
    parser.add_argument("--visual-max-attempts-per-scene", type=int, default=30)


def _prepare(args: argparse.Namespace) -> Path:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    visual_source = prepare_visual_source(
        args.input,
        args.output_dir,
        oda_exe=args.oda_exe,
        libredwg_exe=args.libredwg_exe,
        oda_version=args.oda_version,
        dpi=args.dpi,
        layout_name=args.layout,
        svg_max_dimension=args.svg_max_dimension,
        raster_max_dimension=args.raster_max_dimension,
    )
    manifest = create_tile_manifest(
        visual_source,
        args.output_dir,
        tile_size=args.tile_size,
        overlap=args.overlap,
        overview_max=args.overview_max,
        dpi=args.dpi,
    )
    print(manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DWG 全景对象识别：墙体、窗户、门和家具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="渲染输入并生成 overview、tiles、manifest.json")
    _add_prepare_arguments(prepare)

    analyze = subparsers.add_parser("analyze", help="读取 manifest 并调用视觉模型统计卫生间")
    analyze.add_argument("--manifest", required=True, type=Path)
    analyze.add_argument("--output-json", type=Path)
    analyze.add_argument("--model")
    analyze.add_argument("--image-detail", choices=["low", "medium", "high", "auto", "original"])
    analyze.add_argument("--min-confidence", type=float, default=0.60)
    analyze.add_argument("--limit", type=int, help="只分析前 N 个分块，用于试运行")

    run = subparsers.add_parser("run", help="执行 prepare + analyze")
    _add_prepare_arguments(run)
    run.add_argument("--model")
    run.add_argument("--image-detail", choices=["low", "medium", "high", "auto", "original"])
    run.add_argument("--min-confidence", type=float, default=0.60)
    run.add_argument("--limit", type=int)

    detect = subparsers.add_parser("detect", help="输入 DWG 并输出墙体、窗户、门、家具的 detection.json")
    _add_detect_arguments(detect)

    full = subparsers.add_parser("full", help="按新主架构执行 DWG → Scene → SymPointV2 → Scene Graph")
    _add_full_arguments(full)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare":
            _prepare(args)
            return

        if args.command == "analyze":
            result = analyze_manifest(
                args.manifest,
                model=args.model,
                image_detail=args.image_detail,
                min_confidence=args.min_confidence,
                limit=args.limit,
            )
            output = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output_json:
                args.output_json.write_text(output, encoding="utf-8")
                print(args.output_json)
            else:
                print(output)
            return

        if args.command == "detect":
            result = detect_drawing(
                args.input,
                args.output_dir,
                provider_names=[item.strip() for item in args.providers.split(",") if item.strip()],
                geometry_json=args.geometry_json,
                use_autocad=args.autocad,
                autocad_prog_id=args.autocad_prog_id,
                autocad_visible=args.autocad_visible,
                oda_exe=args.oda_exe,
                libredwg_exe=args.libredwg_exe,
                oda_version=args.oda_version,
                layout_name=args.layout,
                dpi=args.dpi,
                svg_max_dimension=args.svg_max_dimension,
                raster_max_dimension=args.raster_max_dimension,
                tile_size=args.tile_size,
                overlap=args.overlap,
                overview_max=args.overview_max,
                mosaic_group_size=args.mosaic_group_size,
                mosaic_max_dimension=args.mosaic_max_dimension,
                limit_mosaics=args.limit_mosaics,
            )
            print(result["output_json"])
            print(json.dumps(result["summary"], ensure_ascii=False))
            return

        if args.command == "full":
            result = run_full_pipeline(
                args.input,
                args.output_dir,
                raw_json=args.raw_json,
                use_autocad=args.autocad,
                autocad_prog_id=args.autocad_prog_id,
                autocad_visible=args.autocad_visible,
                sympoint_url=args.sympoint_url,
                sympoint_mode=args.sympoint_mode,
                vision_provider_names=[item.strip() for item in args.vision_providers.split(",") if item.strip()],
                run_vision=args.run_vision,
                oda_exe=args.oda_exe,
                libredwg_exe=args.libredwg_exe,
                oda_version=args.oda_version,
                layout=args.layout,
                dpi=args.dpi,
                raster_max_dimension=args.raster_max_dimension,
                job_id=args.job_id,
                visual_max_attempts_per_object=args.visual_max_attempts_per_object,
                visual_max_attempts_per_scene=args.visual_max_attempts_per_scene,
                fusion_provider_name=args.fusion_provider,
                sympoint_tile_size=args.sympoint_tile_size,
                sympoint_tile_overlap=args.sympoint_tile_overlap,
                sympoint_tile_min_primitives=args.sympoint_tile_min_primitives,
                generate_comparison=not args.skip_comparison,
                run_scene_overview=not args.skip_visual_overview,
                legacy_candidate_mode=args.legacy_candidate_mode,
            )
            print(result["output_json"])
            print(json.dumps({
                "job_id": result["job_id"],
                "scene_count": len(result["scenes"]),
                "validation": result["validation"],
                "remote_sympoint": result["remote_sympoint"],
            }, ensure_ascii=False))
            return

        manifest_path = _prepare(args)
        result = analyze_manifest(
            manifest_path,
            model=args.model,
            image_detail=args.image_detail,
            min_confidence=args.min_confidence,
            limit=args.limit,
        )
        output_path = args.output_dir / "analysis.json"
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(output_path)
        print(f"卫生间数量: {result['bathroom_count']}（待复核: {result['review_count']}）")
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        raise SystemExit(f"错误: {exc}") from exc


if __name__ == "__main__":
    main()
