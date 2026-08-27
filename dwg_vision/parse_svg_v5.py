from __future__ import annotations

"""SymPointV2 ``parse_svg_v5.py`` compatible SVG preprocessor.

The cloud training/inference project uses this representation as the bridge
between a cropped Scene SVG and ``SVGDataset.load``.  The core sampling and
field names intentionally follow the cloud v5 script; the local copy removes
the dataset-wide ``mmcv`` runner so it can be used safely for one Scene at a
time on Windows.
"""

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np
from svgpathtools import parse_path


LABEL_NUM = 35
COMMANDS = ["Line", "Arc", "circle", "ellipse"]


def _namespace_prefix(root: ET.Element) -> str:
    tag = str(root.tag)
    return tag[: tag.rfind("}") + 1] if tag.startswith("{") else ""


def _viewbox(root: ET.Element) -> tuple[int, int, int, int]:
    values = [float(value) for value in re.split(r"[\s,]+", str(root.attrib.get("viewBox", "")).strip()) if value]
    if len(values) != 4:
        raise ValueError("SVG viewBox must contain four numbers")
    return tuple(int(value) for value in values)  # type: ignore[return-value]


def _stroke_rgb(element: ET.Element) -> list[int]:
    return list(map(int, re.findall(r"\d+", str(element.attrib.get("stroke", "rgb(0,0,0)")))))[:3] or [0, 0, 0]


def _stroke_width(element: ET.Element) -> float:
    try:
        return float(element.attrib.get("stroke-width", 0.1))
    except (TypeError, ValueError):
        return 0.1


def _semantic_id(element: ET.Element) -> int:
    # Missing labels are the background placeholder used by SymPointV2.
    return int(element.attrib["semanticId"]) - 1 if "semanticId" in element.attrib else LABEL_NUM


def _instance_id(element: ET.Element) -> int:
    try:
        return int(element.attrib.get("instanceId", -1))
    except (TypeError, ValueError):
        return -1


def parse_svg(svg_file: str | Path) -> dict[str, Any]:
    """Convert one Scene-local SVG to the SymPointV2 v5 ``*_s2.json`` shape."""
    svg_file = str(svg_file)
    root = ET.parse(svg_file).getroot()
    namespace = _namespace_prefix(root)
    _minx, _miny, width, height = _viewbox(root)

    commands: list[int] = []
    args: list[list[float]] = []
    lengths: list[float] = []
    semantic_ids: list[int] = []
    instance_ids: list[int] = []
    strokes: list[list[int]] = []
    layer_ids: list[int] = []
    widths: list[float] = []
    instance_info: defaultdict[tuple[int, int], list[float]] = defaultdict(list)

    group_id = 0
    for group in root.iter(namespace + "g"):
        group_id += 1

        for path in group.iter(namespace + "path"):
            try:
                path_repr = parse_path(path.attrib["d"])
            except Exception as exc:
                raise RuntimeError(f"Parse path failed: {svg_file}, {path.attrib.get('d', '')}") from exc
            if not path_repr:
                continue
            path_type = path_repr[0].__class__.__name__
            if path_type not in COMMANDS:
                raise RuntimeError(f"Unsupported SVG path type {path_type!r}: {svg_file}")
            commands.append(COMMANDS.index(path_type))
            lengths.append(float(path_repr.length()))
            layer_ids.append(group_id)
            semantic_id = _semantic_id(path)
            instance_id = _instance_id(path)
            semantic_ids.append(semantic_id)
            instance_ids.append(instance_id)
            strokes.append(_stroke_rgb(path))
            widths.append(_stroke_width(path))
            arg: list[float] = []
            for fraction in (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0):
                point = path_repr.point(fraction)
                arg.extend([float(point.real), float(point.imag)])
            args.append(arg)
            instance_info[(instance_id, semantic_id)].extend(arg)

        for circle in group.iter(namespace + "circle"):
            cx = float(circle.attrib["cx"])
            cy = float(circle.attrib["cy"])
            radius = float(circle.attrib["r"])
            semantic_id = _semantic_id(circle)
            instance_id = _instance_id(circle)
            lengths.append(2.0 * math.pi * radius)
            semantic_ids.append(semantic_id)
            instance_ids.append(instance_id)
            commands.append(COMMANDS.index("circle"))
            layer_ids.append(group_id)
            strokes.append(_stroke_rgb(circle))
            widths.append(_stroke_width(circle))
            arg: list[float] = []
            for theta in (0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0):
                arg.extend([cx + radius * math.cos(theta), cy + radius * math.sin(theta)])
            args.append(arg)
            instance_info[(instance_id, semantic_id)].extend(arg)

        for ellipse in group.iter(namespace + "ellipse"):
            cx = float(ellipse.attrib["cx"])
            cy = float(ellipse.attrib["cy"])
            rx = float(ellipse.attrib["rx"])
            ry = float(ellipse.attrib["ry"])
            a, b = (rx, ry) if rx > ry else (ry, rx)
            semantic_id = _semantic_id(ellipse)
            instance_id = _instance_id(ellipse)
            lengths.append(2.0 * math.pi * b + 4.0 * (a - b))
            commands.append(COMMANDS.index("ellipse"))
            semantic_ids.append(semantic_id)
            instance_ids.append(instance_id)
            layer_ids.append(group_id)
            strokes.append(_stroke_rgb(ellipse))
            widths.append(_stroke_width(ellipse))
            arg = []
            for theta in (0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0):
                arg.extend([cx + a * math.cos(theta), cy + b * math.sin(theta)])
            args.append(arg)
            instance_info[(instance_id, semantic_id)].extend(arg)

    if not (len(args) == len(lengths) == len(semantic_ids) == len(instance_ids) == len(layer_ids) == len(widths)):
        raise ValueError("SymPointV2 SVG primitive arrays have inconsistent lengths")

    obj_cts: list[list[float | int]] = []
    obj_boxes: list[list[float | int]] = []
    for (instance_id, semantic_id), coordinates in instance_info.items():
        if instance_id < 0 or not coordinates:
            continue
        points = np.asarray(coordinates, dtype=float).reshape(-1, 2)
        x1, y1 = np.min(points[:, 0]), np.min(points[:, 1])
        x2, y2 = np.max(points[:, 0]), np.max(points[:, 1])
        obj_cts.append([(x1 + x2) / 2.0, (y1 + y2) / 2.0, 0, instance_id])
        obj_boxes.append([x1, y1, x2, y2, semantic_id])

    return {
        "commands": commands,
        "args": args,
        "lengths": lengths,
        "semanticIds": semantic_ids,
        "instanceIds": instance_ids,
        "width": width,
        "height": height,
        "obj_cts": obj_cts,
        "boxes": obj_boxes,
        "rgb": strokes,
        "layerIds": layer_ids,
        "widths": widths,
    }


def save_json(data: dict[str, Any], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def process(svg_file: str | Path, save_dir: str | Path | None = None) -> Path:
    svg_path = Path(svg_file)
    destination = Path(save_dir) if save_dir is not None else svg_path.parent
    return save_json(parse_svg(svg_path), destination / f"{svg_path.stem}_s2.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Scene SVG files into SymPointV2 v5 JSON")
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--save-dir", type=Path)
    args = parser.parse_args()
    paths = sorted(args.data_dir.glob("*.svg")) if args.data_dir.is_dir() else [args.data_dir]
    for path in paths:
        print(process(path, args.save_dir or path.parent))


if __name__ == "__main__":
    main()
