from __future__ import annotations

"""Vector evidence extraction for DXF and AutoCAD-exported JSON sidecars."""

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .schema import normalize_entity


WALL_TERMS = ("wall", "a-wall", "墙", "墙体", "外墙", "内墙", "墙线")
WINDOW_TERMS = ("window", "窗", "a-glaz", "glaz")
DOOR_TERMS = ("door", "门", "入口", "a-door")
FURNITURE_TERMS = (
    "furniture",
    "furn",
    "家具",
    "bed",
    "sofa",
    "chair",
    "table",
    "desk",
    "cabinet",
    "床",
    "沙发",
    "椅",
    "桌",
    "柜",
)


def _bbox(points: Iterable[tuple[float, float]]) -> list[float]:
    values = list(points)
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    xs = [point[0] for point in values]
    ys = [point[1] for point in values]
    return [min(xs), min(ys), max(xs), max(ys)]


def _center(box: list[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _terms_type(value: str) -> str | None:
    lowered = value.lower()
    for entity_type, terms in (
        ("wall", WALL_TERMS),
        ("window", WINDOW_TERMS),
        ("door", DOOR_TERMS),
        ("furniture", FURNITURE_TERMS),
    ):
        if any(term.lower() in lowered for term in terms):
            return entity_type
    return None


def _candidate(
    *,
    entity_type: str,
    bbox: list[float],
    subtype: str = "unknown",
    polygon: list[list[float]] | None = None,
    rotation_deg: float = 0.0,
    dimensions: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = normalize_entity(
        {
            "type": entity_type,
            "subtype": subtype,
            "bbox_px": bbox,
            "polygon_px": polygon or [],
            "rotation_deg": rotation_deg,
            "dimensions": dimensions or {},
            "confidence": 0.76,
            "evidence": evidence or {},
            "provenance": provenance or {},
        },
        default_source="vector",
    )
    result["coordinate_space"] = "world"
    return result


def _line_points(entity: Any) -> list[tuple[float, float]]:
    start = entity.dxf.start
    end = entity.dxf.end
    return [(float(start.x), float(start.y)), (float(end.x), float(end.y))]


def _polyline_points(entity: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if entity.dxftype() == "LWPOLYLINE":
        points = [(float(point[0]), float(point[1])) for point in entity.get_points("xy")]
    else:
        points = [(float(vertex.dxf.location.x), float(vertex.dxf.location.y)) for vertex in entity.vertices]
    return points


def _circle_points(entity: Any) -> tuple[list[float], list[list[float]]]:
    center = entity.dxf.center
    radius = float(entity.dxf.radius)
    cx, cy = float(center.x), float(center.y)
    return [cx - radius, cy - radius, cx + radius, cy + radius], []


def _insert_points(document: Any, entity: Any) -> tuple[list[float], list[list[float]]]:
    """Approximate a block's extents in WCS for evidence and validation."""
    insert = entity.dxf.insert
    scale_x = float(getattr(entity.dxf, "xscale", 1.0) or 1.0)
    scale_y = float(getattr(entity.dxf, "yscale", 1.0) or 1.0)
    angle = math.radians(float(getattr(entity.dxf, "rotation", 0.0) or 0.0))
    cosine, sine = math.cos(angle), math.sin(angle)
    points: list[tuple[float, float]] = []
    try:
        block = document.blocks.get(entity.dxf.name)
    except Exception:
        block = None
    if block is not None:
        for child in block:
            try:
                if child.dxftype() == "LINE":
                    child_points = _line_points(child)
                elif child.dxftype() in {"LWPOLYLINE", "POLYLINE"}:
                    child_points = _polyline_points(child)
                elif child.dxftype() == "CIRCLE":
                    box, _ = _circle_points(child)
                    child_points = [(box[0], box[1]), (box[2], box[3])]
                else:
                    continue
                for x, y in child_points:
                    tx = x * scale_x
                    ty = y * scale_y
                    points.append(
                        (
                            float(insert.x) + tx * cosine - ty * sine,
                            float(insert.y) + tx * sine + ty * cosine,
                        )
                    )
            except (AttributeError, TypeError, ValueError):
                continue
    if not points:
        x, y = float(insert.x), float(insert.y)
        points = [(x, y), (x + scale_x, y + scale_y)]
    return _bbox(points), [[x, y] for x, y in points]


def _extract_dxf(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    try:
        import ezdxf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("DXF 几何提取需要安装 ezdxf") from exc

    document = ezdxf.readfile(path)
    modelspace = document.modelspace()
    entities: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    segments: list[tuple[tuple[float, float], tuple[float, float], str, str]] = []

    for entity in modelspace:
        dxftype = entity.dxftype()
        layer = str(getattr(entity.dxf, "layer", ""))
        handle = str(getattr(entity.dxf, "handle", ""))
        hint = _terms_type(layer)
        evidence = {"method": "dxf", "layer": layer, "handle": handle, "entity_type": dxftype}
        provenance = {"source": "dxf", "handle": handle, "layer": layer}

        if dxftype == "LINE":
            points = _line_points(entity)
            segments.append((points[0], points[1], layer, handle))
            if hint:
                length = math.dist(points[0], points[1])
                entities.append(
                    _candidate(
                        entity_type=hint,
                        bbox=_bbox(points),
                        subtype=dxftype.lower(),
                        polygon=[[x, y] for x, y in points],
                        dimensions={"length": length},
                        evidence=evidence,
                        provenance=provenance,
                    )
                )
        elif dxftype in {"LWPOLYLINE", "POLYLINE"}:
            points = _polyline_points(entity)
            if len(points) < 2:
                continue
            if hint:
                entities.append(
                    _candidate(
                        entity_type=hint,
                        bbox=_bbox(points),
                        subtype="closed_polyline" if bool(getattr(entity, "closed", False)) else "polyline",
                        polygon=[[x, y] for x, y in points],
                        dimensions={"perimeter_vertices": len(points)},
                        evidence=evidence,
                        provenance=provenance,
                    )
                )
        elif dxftype == "CIRCLE":
            box, _ = _circle_points(entity)
            if hint:
                entities.append(
                    _candidate(
                        entity_type=hint,
                        bbox=box,
                        subtype="circle",
                        dimensions={"radius": float(entity.dxf.radius)},
                        evidence=evidence,
                        provenance=provenance,
                    )
                )
        elif dxftype == "INSERT":
            block_name = str(getattr(entity.dxf, "name", ""))
            insert_hint = _terms_type(f"{layer} {block_name}")
            if insert_hint:
                box, polygon = _insert_points(document, entity)
                entities.append(
                    _candidate(
                        entity_type=insert_hint,
                        bbox=box,
                        subtype=block_name or "insert",
                        polygon=polygon,
                        rotation_deg=float(getattr(entity.dxf, "rotation", 0.0) or 0.0),
                        dimensions={
                            "scale_x": float(getattr(entity.dxf, "xscale", 1.0) or 1.0),
                            "scale_y": float(getattr(entity.dxf, "yscale", 1.0) or 1.0),
                        },
                        evidence={**evidence, "block_name": block_name},
                        provenance={**provenance, "block_name": block_name},
                    )
                )
        elif dxftype in {"TEXT", "MTEXT", "ATTRIB"}:
            text = str(getattr(entity.dxf, "text", "") or "")
            insert = getattr(entity.dxf, "insert", None)
            if insert is not None:
                x, y = float(insert.x), float(insert.y)
                height = float(getattr(entity.dxf, "height", 100.0) or 100.0)
                annotations.append(
                    {
                        "text": text,
                        "bbox_world": [x, y, x + max(height, 1.0), y + max(height, 1.0)],
                        "position_world": [x, y],
                        "source": "dxf",
                        "layer": layer,
                        "handle": handle,
                    }
                )

    # Unnamed wall layers are common.  Only promote long, nearly parallel
    # line pairs with a short gap; this avoids treating every annotation line
    # as a wall while still recovering simple architectural plans.
    entities.extend(_infer_wall_pairs(segments))
    try:
        extmin = document.header.get("$EXTMIN")
        extmax = document.header.get("$EXTMAX")
        bounds = [float(extmin[0]), float(extmin[1]), float(extmax[0]), float(extmax[1])]
    except (AttributeError, KeyError, TypeError, ValueError):
        bounds = _bbox(point for segment in segments for point in segment[:2])
    return entities, annotations, {"space": "world", "units": "drawing_units", "world_bounds": bounds}


def _infer_wall_pairs(
    segments: list[tuple[tuple[float, float], tuple[float, float], str, str]]
) -> list[dict[str, Any]]:
    inferred: list[dict[str, Any]] = []
    for index, first in enumerate(segments):
        (ax, ay), (bx, by), layer, handle = first
        length = math.dist((ax, ay), (bx, by))
        if length <= 0:
            continue
        ux, uy = (bx - ax) / length, (by - ay) / length
        for second in segments[index + 1 :]:
            (cx, cy), (dx, dy), second_layer, second_handle = second
            other_length = math.dist((cx, cy), (dx, dy))
            if other_length <= 0 or abs(other_length - length) > length * 0.35:
                continue
            cross = abs(ux * (dy - cy) - uy * (dx - cx)) / max(1.0, other_length)
            if cross > 0.08:
                continue
            gap = abs(ux * (cy - ay) - uy * (cx - ax))
            if gap <= 1.0 or gap > max(1200.0, length * 0.12):
                continue
            midpoint = ((ax + bx + cx + dx) / 4.0, (ay + by + cy + dy) / 4.0)
            box = _bbox([(ax, ay), (bx, by), (cx, cy), (dx, dy)])
            inferred.append(
                _candidate(
                    entity_type="wall",
                    bbox=box,
                    subtype="parallel_line_pair",
                    dimensions={"length": max(length, other_length), "thickness": gap},
                    evidence={"method": "parallel_line_pair", "source_handles": [handle, second_handle]},
                    provenance={"source": "geometry_heuristic", "source_handles": [handle, second_handle]},
                )
            )
            break
    return inferred


def load_geometry_sidecar(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_entities = payload.get("entities") or payload.get("objects") or []
    entities = [normalize_entity(item, default_source="autocad") for item in raw_entities]
    for entity in entities:
        entity["coordinate_space"] = payload.get("coordinate_space", "world")
    return entities, list(payload.get("annotations") or payload.get("texts") or []), dict(
        payload.get("coordinate_system") or {"space": "world", "units": "drawing_units"}
    )


def extract_vector_evidence(
    input_path: Path,
    *,
    geometry_json: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Extract available vector evidence; never fails a visual-only run."""
    if geometry_json and geometry_json.is_file():
        return load_geometry_sidecar(geometry_json)
    if input_path.suffix.lower() == ".dxf":
        return _extract_dxf(input_path)
    return [], [], {"space": "pixel", "units": "unknown", "world_bounds": None}
