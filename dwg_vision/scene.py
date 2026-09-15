from __future__ import annotations

"""DWG raw-data normalization, multi-frame Scene building and Bundle output."""

import hashlib
import json
import math
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .contracts import SCHEMA_VERSIONS, require_bbox, validate_job_manifest, validate_raw, validate_scene
from .sympoint_preprocess import preprocess_scene_svg


TEXT_ROLE_TERMS: tuple[tuple[str, str], ...] = (
    ("图例", "legend"),
    ("legend", "legend"),
    ("标题", "title_block"),
    ("title", "title_block"),
    ("卫生间", "room_label"),
    ("厕所", "room_label"),
    ("厨房", "room_label"),
    ("客厅", "room_label"),
    ("卧室", "room_label"),
    ("bedroom", "room_label"),
    ("kitchen", "room_label"),
    ("bathroom", "room_label"),
    ("尺寸", "dimension"),
    ("标高", "dimension"),
    ("dimension", "dimension"),
    ("门", "door_window_tag"),
    ("窗", "door_window_tag"),
    ("door", "door_window_tag"),
    ("window", "door_window_tag"),
    ("马桶", "equipment_label"),
    ("坐便", "equipment_label"),
    ("洗手", "equipment_label"),
    ("淋浴", "equipment_label"),
    ("sink", "equipment_label"),
    ("toilet", "equipment_label"),
    ("床", "equipment_label"),
    ("沙发", "equipment_label"),
    ("柜", "equipment_label"),
    ("家具", "equipment_label"),
    ("furniture", "equipment_label"),
)


def _bbox_from_points(points: Iterable[Iterable[float]]) -> list[float]:
    values = [[float(point[0]), float(point[1])] for point in points if len(point) >= 2]
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    xs = [point[0] for point in values]
    ys = [point[1] for point in values]
    return [min(xs), min(ys), max(xs), max(ys)]


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersects(first: list[float], second: list[float]) -> bool:
    return not (first[2] < second[0] or second[2] < first[0] or first[3] < second[1] or second[3] < first[1])


def _expand(box: list[float], margin: float) -> list[float]:
    return [box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin]


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_role(text: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    normalized = str(text or "").strip().lower()
    for term, role in TEXT_ROLE_TERMS:
        if term.lower() in normalized:
            return role
    return "general_annotation"


def _raw_entity_bbox(entity: dict[str, Any]) -> list[float]:
    return require_bbox(entity.get("bbox_world") or entity.get("bbox_px") or [0, 0, 0, 0], "entity bbox")


def _raw_annotation_bbox(annotation: dict[str, Any]) -> list[float]:
    return require_bbox(annotation.get("bbox_world") or annotation.get("bbox_px") or [0, 0, 0, 0], "annotation bbox")


def _geometry_entities(raw: dict[str, Any]) -> list[dict[str, Any]]:
    raw_entities = raw.get("raw_entities")
    if isinstance(raw_entities, list) and raw_entities:
        return raw_entities
    entities = raw.get("entities")
    return entities if isinstance(entities, list) else []


def _world_to_local(box: list[float], origin: tuple[float, float]) -> list[float]:
    return [box[0] - origin[0], box[1] - origin[1], box[2] - origin[0], box[3] - origin[1]]


def _points_for_entity(entity: dict[str, Any], bbox_world: list[float], origin: tuple[float, float]) -> list[list[float]]:
    raw_points = entity.get("polygon_world") or entity.get("polygon_px") or entity.get("points_world") or entity.get("points")
    if isinstance(raw_points, list):
        points: list[list[float]] = []
        for point in raw_points:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append([float(point[0]) - origin[0], float(point[1]) - origin[1]])
        if points:
            # ActiveX/.NET block references often expose only their insertion
            # point while their GeometricExtents still contain the real block
            # footprint.  SymPointV2 only accepts path/circle/ellipse
            # primitives, so use a deterministic footprint proxy rather than
            # emitting a one-point path that parse_svg_v5 would silently drop.
            command = _command_for_entity(entity)
            if command == "polyline" and len(points) == 1 and (
                bbox_world[2] - bbox_world[0] > 1e-9 and bbox_world[3] - bbox_world[1] > 1e-9
            ):
                local = _world_to_local(bbox_world, origin)
                return [[local[0], local[1]], [local[2], local[1]], [local[2], local[3]], [local[0], local[3]]]
            return points
    local = _world_to_local(bbox_world, origin)
    return [[local[0], local[1]], [local[2], local[1]], [local[2], local[3]], [local[0], local[3]]]


def _sampled_points(entity: dict[str, Any], bbox_world: list[float], origin: tuple[float, float]) -> list[list[float]]:
    """Create stable path samples for COM/.NET circle, arc and ellipse objects."""
    points = _points_for_entity(entity, bbox_world, origin)
    command = _command_for_entity(entity)
    dimensions = dict(entity.get("dimensions") or {})
    if command not in {"arc", "ellipse"}:
        return points
    # Exploded .NET ARC records sometimes carry a bbox rectangle as their
    # fallback polygon.  That rectangle is not curve geometry.  Prefer the
    # authoritative WCS centre/endpoints and normal whenever they exist.
    if command == "arc":
        center = dimensions.get("center_world")
        start_point = dimensions.get("start_point_world")
        end_point = dimensions.get("end_point_world")
        if (
            isinstance(center, (list, tuple)) and len(center) >= 2
            and isinstance(start_point, (list, tuple)) and len(start_point) >= 2
            and isinstance(end_point, (list, tuple)) and len(end_point) >= 2
        ):
            center_x, center_y = float(center[0]), float(center[1])
            start_angle = math.atan2(float(start_point[1]) - center_y, float(start_point[0]) - center_x)
            end_angle = math.atan2(float(end_point[1]) - center_y, float(end_point[0]) - center_x)
            span = float(dimensions.get("total_angle") or 0.0)
            if span <= 1e-9:
                span = abs((end_angle - start_angle + math.pi) % (2.0 * math.pi) - math.pi)
            normal = dimensions.get("normal_world") or []
            direction = -1.0 if len(normal) >= 3 and float(normal[2]) < 0.0 else 1.0
            radius = math.hypot(float(start_point[0]) - center_x, float(start_point[1]) - center_y)
            count = max(8, min(32, int(abs(span) / (math.pi / 12.0)) + 1))
            return [[
                center_x + radius * math.cos(start_angle + direction * span * index / (count - 1)) - origin[0],
                center_y + radius * math.sin(start_angle + direction * span * index / (count - 1)) - origin[1],
            ] for index in range(count)]
    if len(points) != 1:
        return points
    center_world = [
        (bbox_world[0] + bbox_world[2]) / 2.0,
        (bbox_world[1] + bbox_world[3]) / 2.0,
    ]
    if command == "ellipse":
        radius_x = max(1e-6, (bbox_world[2] - bbox_world[0]) / 2.0)
        radius_y = max(1e-6, (bbox_world[3] - bbox_world[1]) / 2.0)
        return [[
            center_world[0] + radius_x * math.cos(2.0 * math.pi * index / 32.0) - origin[0],
            center_world[1] + radius_y * math.sin(2.0 * math.pi * index / 32.0) - origin[1],
        ] for index in range(32)]
    radius = float(dimensions.get("radius") or max(bbox_world[2] - bbox_world[0], bbox_world[3] - bbox_world[1]) / 2.0)
    start = float(dimensions.get("start_angle") or 0.0)
    end = float(dimensions.get("end_angle") or (2.0 * math.pi))
    if end <= start:
        end += 2.0 * math.pi
    count = max(8, min(32, int(abs(end - start) / (math.pi / 12.0)) + 1))
    return [[
        center_world[0] + radius * math.cos(start + (end - start) * index / (count - 1)) - origin[0],
        center_world[1] + radius * math.sin(start + (end - start) * index / (count - 1)) - origin[1],
    ] for index in range(count)]


def _command_for_entity(entity: dict[str, Any]) -> str:
    raw = " ".join(
        str(entity.get(key) or "") for key in ("command", "subtype", "entity_type", "object_name")
    ).lower()
    if "circle" in raw:
        return "circle"
    if "ellipse" in raw:
        return "ellipse"
    if "arc" in raw:
        return "arc"
    return "polyline"


def _primitive_from_entity(entity: dict[str, Any], scene_id: str, index: int, origin: tuple[float, float]) -> dict[str, Any]:
    bbox_world = _raw_entity_bbox(entity)
    points_local = _sampled_points(entity, bbox_world, origin)
    local_bbox = _world_to_local(bbox_world, origin)
    source_entity_id = str(entity.get("entity_id") or entity.get("id") or entity.get("handle") or f"entity_{index:05d}")
    primitive_id = f"{scene_id}_prim_{index:05d}"
    dimensions = dict(entity.get("dimensions") or {})
    dimensions.setdefault("width", max(0.0, bbox_world[2] - bbox_world[0]))
    dimensions.setdefault("height", max(0.0, bbox_world[3] - bbox_world[1]))
    return {
        "primitive_id": primitive_id,
        "source_entity_id": source_entity_id,
        "handle": str(entity.get("handle") or entity.get("provenance", {}).get("handle") or ""),
        "type": str(entity.get("type") or "unknown"),
        "subtype": str(entity.get("subtype") or entity.get("entity_type") or ""),
        "entity_type": str(entity.get("entity_type") or ""),
        "command": _command_for_entity(entity),
        "points_local": points_local,
        "bbox_local": local_bbox,
        "bbox_world": bbox_world,
        "center_local": [(local_bbox[0] + local_bbox[2]) / 2.0, (local_bbox[1] + local_bbox[3]) / 2.0],
        "length": float(dimensions.get("length") or math.hypot(local_bbox[2] - local_bbox[0], local_bbox[3] - local_bbox[1])),
        "width": float(entity.get("lineweight") or entity.get("width") or 0.1),
        "rotation_deg": float(entity.get("rotation_deg") or 0.0),
        "dimensions": dimensions,
        "is_frame": bool(entity.get("is_frame", False)),
        "layer": str(entity.get("layer") or entity.get("provenance", {}).get("layer") or ""),
        "color": entity.get("color", 256),
        "source": dict(entity.get("provenance") or {}),
    }


def detect_frames(raw: dict[str, Any], *, margin: float = 0.0) -> list[dict[str, Any]]:
    """Return explicit frames/viewports, or one deterministic fallback Scene."""
    frames: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("frames") or raw.get("frame_candidates") or raw.get("scenes") or []):
        if not isinstance(item, dict):
            continue
        box = item.get("world_bbox") or item.get("bbox") or item.get("bbox_world")
        if not box:
            continue
        bbox = _expand(require_bbox(box, "frame bbox"), margin)
        if _area(bbox) <= 0:
            continue
        frames.append({
            "scene_id": str(item.get("scene_id") or f"scene_{index + 1:04d}"),
            "frame_type": str(item.get("frame_type") or item.get("type") or "explicit"),
            "world_bbox": bbox,
            "layout_id": item.get("layout_id"),
        })
    if frames:
        return frames

    raw_bounds = (raw.get("coordinate_system") or {}).get("world_bounds") or raw.get("world_bounds")
    if raw_bounds:
        bbox = require_bbox(raw_bounds, "world_bounds")
    else:
        source_entities = _geometry_entities(raw)
        boxes = [_raw_entity_bbox(entity) for entity in source_entities if isinstance(entity, dict)]
        bbox = _bbox_from_points([[box[0], box[1]] for box in boxes] + [[box[2], box[3]] for box in boxes])
    bbox = _expand(bbox, margin)
    if _area(bbox) <= 0:
        raise ValueError("无法从 DWG 原生数据确定有效的 Scene 范围")
    return [{"scene_id": "scene_0001", "frame_type": "fallback", "world_bbox": bbox, "layout_id": None}]


def extract_scene_texts(raw: dict[str, Any], scene: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    scene_bbox = require_bbox(scene["world_bounds"], "scene.world_bounds")
    for index, annotation in enumerate(raw.get("annotations") or raw.get("texts") or []):
        if not isinstance(annotation, dict):
            continue
        bbox_world = _raw_annotation_bbox(annotation)
        if not _intersects(scene_bbox, bbox_world):
            continue
        text = str(annotation.get("text") or annotation.get("text_string") or "").strip()
        if not text:
            continue
        text_id = str(annotation.get("text_id") or annotation.get("id") or annotation.get("handle") or f"text_{index:05d}")
        result.append({
            "text_id": text_id,
            "handle": str(annotation.get("handle") or ""),
            "scene_id": scene["scene_id"],
            "text": text,
            "normalized_text": re.sub(r"\s+", " ", text),
            "role": _text_role(text, annotation.get("role")),
            "bbox_world": bbox_world,
            "position_world": list(annotation.get("position_world") or [(bbox_world[0] + bbox_world[2]) / 2.0, (bbox_world[1] + bbox_world[3]) / 2.0]),
            "layer": str(annotation.get("layer") or ""),
            "style": dict(annotation.get("style") or {}),
            "source": str(annotation.get("source") or "autocad"),
        })
    return result


def build_scene(raw: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any]:
    world_bounds = require_bbox(frame["world_bbox"], "frame.world_bbox")
    origin = (world_bounds[0], world_bounds[1])
    primitives: list[dict[str, Any]] = []
    native_entity_ids: list[str] = []
    source_entities = _geometry_entities(raw)
    for index, entity in enumerate(source_entities):
        if not isinstance(entity, dict):
            continue
        if entity.get("is_frame"):
            continue
        bbox = _raw_entity_bbox(entity)
        if not _intersects(world_bounds, bbox):
            continue
        primitive = _primitive_from_entity(entity, str(frame["scene_id"]), len(primitives), origin)
        primitives.append(primitive)
        native_entity_ids.append(primitive["source_entity_id"])
    local_bounds = [0.0, 0.0, world_bounds[2] - world_bounds[0], world_bounds[3] - world_bounds[1]]
    scene = {
        "schema_version": SCHEMA_VERSIONS["scene"],
        "scene_id": str(frame["scene_id"]),
        "frame_type": str(frame.get("frame_type") or "unknown"),
        "layout_id": frame.get("layout_id"),
        "coordinate_space": "scene_local",
        "world_bounds": world_bounds,
        "local_bounds": local_bounds,
        "local_to_world": {"origin": list(origin), "scale": 1.0, "rotation_deg": 0.0},
        "primitives": primitives,
        "native_entity_ids": native_entity_ids,
        "texts": extract_scene_texts(raw, {"scene_id": frame["scene_id"], "world_bounds": world_bounds}),
        # Native DIMENSION objects stay in ``texts`` for high-priority text
        # evidence and rule validation, but are omitted from the visual SVG by
        # default because their measurement text can be thousands of drawing
        # units high and overwhelm the object image.
        "render": {
            "status": "pending",
            "image_path": None,
            "text_policy": "omit_dimension_annotations",
        },
    }
    validate_scene(scene)
    return scene


def _svg_color(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return f"rgb({int(value[0])},{int(value[1])},{int(value[2])})"
    return "rgb(0,0,0)"


def scene_to_svg(scene: dict[str, Any], output_path: Path) -> Path:
    validate_scene(scene)
    width = max(1.0, float(scene["local_bounds"][2]))
    height = max(1.0, float(scene["local_bounds"][3]))
    # DWG model space is Y-up whereas SVG/raster space is Y-down.
    def svg_y(value: float) -> float:
        return height - float(value)
    root = ET.Element("svg", {
        "version": "1.1",
        "viewBox": f"0 0 {width:g} {height:g}",
        "xmlns": "http://www.w3.org/2000/svg",
    })
    # SymPointV2 uses the SVG group index as a layer feature.  Keeping every
    # primitive in one group collapses all CAD layers to layerId=1 and causes
    # a severe train/inference distribution shift.  Create one SVG group per
    # source CAD layer in first-seen order; annotations are placed in a
    # separate group after geometry and are ignored by parse_svg_v5.
    layer_groups: dict[str, ET.Element] = {}
    for primitive in scene["primitives"]:
        layer_name = str(primitive.get("layer") or "__unlayered__")
        group = layer_groups.get(layer_name)
        if group is None:
            group = ET.SubElement(root, "g", {"data-layer": layer_name})
            layer_groups[layer_name] = group
        points = primitive["points_local"]
        # AutoCAD can expose zero-length construction fragments (including
        # exploded anonymous-block remnants).  They have no drawable CAD
        # geometry but svgpathtools rejects a Path made solely from them.
        if primitive["command"] not in {"circle", "ellipse"} and (
            len(points) < 2 or all(point == points[0] for point in points[1:])
        ):
            continue
        if primitive["command"] == "circle":
            box = primitive["bbox_local"]
            center_x = (box[0] + box[2]) / 2.0
            center_y = svg_y((box[1] + box[3]) / 2.0)
            radius = max(abs(box[2] - box[0]), abs(box[3] - box[1])) / 2.0
            element = ET.SubElement(group, "circle", {
                "cx": f"{center_x:g}", "cy": f"{center_y:g}", "r": f"{radius:g}",
            })
        elif primitive["command"] == "ellipse":
            box = primitive["bbox_local"]
            element = ET.SubElement(group, "ellipse", {
                "cx": f"{(box[0] + box[2]) / 2.0:g}",
                "cy": f"{svg_y((box[1] + box[3]) / 2.0):g}",
                "rx": f"{max(0.01, (box[2] - box[0]) / 2.0):g}",
                "ry": f"{max(0.01, (box[3] - box[1]) / 2.0):g}",
            })
        else:
            commands = [f"M {points[0][0]:g},{svg_y(points[0][1]):g}"]
            commands.extend(f"L {point[0]:g},{svg_y(point[1]):g}" for point in points[1:])
            element = ET.SubElement(group, "path", {"d": " ".join(commands)})
        element.set("fill", "none")
        element.set("stroke", _svg_color(primitive.get("color")))
        element.set("stroke-width", str(max(0.1, float(primitive.get("width") or 0.1))))
        element.set("data-primitive-id", primitive["primitive_id"])
        element.set("data-entity-id", primitive["source_entity_id"])
        # Do not emit fake ground-truth labels.  parse_svg_v5 treats missing
        # semanticId/instanceId as the background/-1 placeholders expected by
        # inference; emitting semanticId=35 would be shifted to class 34.
    annotation_group: ET.Element | None = None
    for text in scene.get("texts", []):
        if text.get("role") == "dimension":
            continue
        if annotation_group is None:
            annotation_group = ET.SubElement(root, "g", {"data-layer": "__annotations__"})
        bbox = _raw_annotation_bbox(text)
        origin = scene["local_to_world"]["origin"]
        position = text.get("position_world") or [(bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0]
        font_size = max(0.1, float(text.get("height") or text.get("style", {}).get("height") or max(1.0, bbox[3] - bbox[1])))
        label = ET.SubElement(annotation_group, "text", {
            "x": f"{float(position[0]) - float(origin[0]):g}",
            "y": f"{svg_y(float(position[1]) - float(origin[1])):g}",
            "font-size": f"{font_size:g}",
            "fill": "rgb(40,40,40)",
            "data-text-id": str(text.get("text_id") or ""),
        })
        label.text = str(text.get("text") or "")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path


def build_scene_bundle(
    raw: dict[str, Any],
    output_dir: Path,
    *,
    source_path: Path | None = None,
    job_id: str = "job_local_0001",
    margin: float = 0.0,
    tile_size: float | None = None,
    tile_overlap: float = 0.0,
    tile_min_primitives: int = 0,
) -> Path:
    """Build an API-compatible Bundle ZIP from AutoCAD raw JSON."""
    validate_raw(raw)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_name = source_path.name if source_path else str((raw.get("source") or {}).get("filename") or "drawing.dwg")
    source_sha = _source_hash(source_path) if source_path and source_path.is_file() else str((raw.get("source") or {}).get("sha256") or "")
    scenes_root = output_dir / "scenes"
    scenes_root.mkdir(parents=True, exist_ok=True)
    manifest_scenes: list[dict[str, Any]] = []
    frames = detect_frames(raw, margin=margin)
    if tile_size is not None:
        if tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if tile_overlap < 0 or tile_overlap >= tile_size:
            raise ValueError("tile_overlap must be non-negative and smaller than tile_size")
        if tile_min_primitives < 0:
            raise ValueError("tile_min_primitives must be non-negative")
        step = tile_size - tile_overlap
        tiled_frames: list[dict[str, Any]] = []
        source_entities = _geometry_entities(raw)
        for frame in frames:
            left, bottom, right, top = require_bbox(frame["world_bbox"], "frame.world_bbox")
            y = bottom
            row = 0
            while y < top:
                x = left
                column = 0
                while x < right:
                    tile_right = min(right, x + tile_size)
                    tile_top = min(top, y + tile_size)
                    tile_bounds = [x, y, tile_right, tile_top]
                    primitive_count = sum(
                        1
                        for entity in source_entities
                        if isinstance(entity, dict) and not entity.get("is_frame")
                        and _intersects(tile_bounds, _raw_entity_bbox(entity))
                    )
                    if primitive_count >= tile_min_primitives:
                        tiled_frames.append({
                        **frame,
                        "scene_id": f"{frame['scene_id']}_tile_r{row:02d}_c{column:02d}",
                        "frame_type": "sympoint_focus_tile",
                        "world_bbox": tile_bounds,
                        })
                    x += step
                    column += 1
                y += step
                row += 1
        frames = tiled_frames
    for frame in frames:
        scene = build_scene(raw, frame)
        scene_dir = scenes_root / scene["scene_id"]
        scene_dir.mkdir(parents=True, exist_ok=True)
        scene_path = scene_dir / "scene.json"
        scene_path.write_text(json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
        scene_svg_path = scene_to_svg(scene, scene_dir / "scene.svg")
        preprocess_scene_svg(scene_svg_path, scene_dir / "scene_s2.json")
        manifest_scenes.append({
            "scene_id": scene["scene_id"],
            "path": f"scenes/{scene['scene_id']}",
            "input_format": "svg",
            "primitive_count": len(scene["primitives"]),
            "preprocessor": "parse_svg_v5",
        })
    manifest = {
        "schema_version": SCHEMA_VERSIONS["job"],
        "job_id": job_id,
        "source": {"filename": source_name, "sha256": source_sha},
        "model": {"name": "SymPointV2", "config": "configs/svg/svg_pointT.yaml", "weights": "best.pth"},
        "scenes": manifest_scenes,
    }
    validate_job_manifest(manifest)
    manifest_path = output_dir / "job_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = output_dir / "job_bundle.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(manifest_path, "job_bundle/job_manifest.json")
        for scene in manifest_scenes:
            scene_dir = output_dir / scene["path"]
            archive.write(scene_dir / "scene.json", f"job_bundle/{scene['path']}/scene.json")
            archive.write(scene_dir / "scene.svg", f"job_bundle/{scene['path']}/scene.svg")
            archive.write(scene_dir / "scene_s2.json", f"job_bundle/{scene['path']}/scene_s2.json")
    return zip_path
