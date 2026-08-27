from __future__ import annotations

"""Local preflight for the exact per-Scene SymPointV2 SVG contract."""

import json
from pathlib import Path
from typing import Any

from .parse_svg_v5 import parse_svg


REQUIRED_ARRAYS = ("commands", "args", "lengths", "semanticIds", "instanceIds", "layerIds", "widths")


def preprocess_scene_svg(svg_path: Path, output_path: Path | None = None) -> dict[str, Any]:
    """Parse one already-cropped Scene SVG and optionally persist ``scene_s2.json``."""
    parsed = parse_svg(svg_path)
    count = len(parsed["args"])
    for field in REQUIRED_ARRAYS:
        if len(parsed.get(field, [])) != count:
            raise ValueError(f"{svg_path}: {field} length does not match args ({count})")
    if int(parsed["width"]) <= 0 or int(parsed["height"]) <= 0:
        raise ValueError(f"{svg_path}: viewBox width/height must be positive")
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return parsed
