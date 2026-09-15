from __future__ import annotations

"""Geometry-only closed-region analysis for exploded CAD linework.

This module deliberately does not assign semantic classes.  It detects small
connected groups containing closed polylines or graph cycles and returns the
original primitive IDs so a later Agent can distinguish a fixture from a door,
wall detail, or a drawing artifact.
"""

import hashlib
import math
from collections import defaultdict
from typing import Any, Iterable


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in values):
        return None
    return [min(values[0], values[2]), min(values[1], values[3]), max(values[0], values[2]), max(values[1], values[3])]


def _union_bbox(boxes: Iterable[list[float]]) -> list[float] | None:
    values = [box for box in (_bbox(item) for item in boxes) if box is not None]
    if not values:
        return None
    return [
        min(item[0] for item in values),
        min(item[1] for item in values),
        max(item[2] for item in values),
        max(item[3] for item in values),
    ]


def _world_points(primitive: dict[str, Any], scene: dict[str, Any]) -> list[list[float]]:
    direct = primitive.get("points_world")
    if isinstance(direct, list) and direct:
        return [[float(point[0]), float(point[1])] for point in direct if isinstance(point, (list, tuple)) and len(point) >= 2]
    origin = (scene.get("local_to_world") or {}).get("origin") or [0.0, 0.0]
    points = primitive.get("points_local") or []
    result: list[list[float]] = []
    for point in points:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            result.append([float(point[0]) + float(origin[0]), float(point[1]) + float(origin[1])])
    return result


def _endpoints(primitive: dict[str, Any], scene: dict[str, Any]) -> list[list[float]]:
    points = _world_points(primitive, scene)
    if not points:
        return []
    return [points[0], points[-1]]


def _distance(first: list[float], second: list[float]) -> float:
    return math.hypot(float(first[0]) - float(second[0]), float(first[1]) - float(second[1]))


def _bbox_gap(first: list[float], second: list[float]) -> float:
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def _is_wall_like(primitive: dict[str, Any]) -> bool:
    source = primitive.get("source") or {}
    text = " ".join(
        str(primitive.get(key) or "")
        for key in ("type", "subtype", "entity_type", "layer")
    ) + " " + str(source.get("layer") or "")
    normalized = text.casefold()
    return any(term in normalized for term in ("wall", "墙", "axis", "轴网", "dimension", "标注"))


def _is_closed_primitive(primitive: dict[str, Any], scene: dict[str, Any], tolerance: float) -> bool:
    dimensions = primitive.get("dimensions") or {}
    if primitive.get("closed") is True or dimensions.get("closed") is True:
        return True
    points = _world_points(primitive, scene)
    return len(points) >= 3 and _distance(points[0], points[-1]) <= tolerance


def _polygon_area(points: list[list[float]]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) / 2.0


class _UnionFind:
    def __init__(self, count: int) -> None:
        self.parent = list(range(count))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self.parent[second_root] = first_root


def _cycle_rank(primitives: list[dict[str, Any]], scene: dict[str, Any], tolerance: float) -> int:
    nodes: dict[tuple[int, int], int] = {}
    edges: list[tuple[int, int]] = []

    def node(point: list[float]) -> int:
        key = (round(point[0] / tolerance), round(point[1] / tolerance))
        if key not in nodes:
            nodes[key] = len(nodes)
        return nodes[key]

    for primitive in primitives:
        points = _world_points(primitive, scene)
        if len(points) < 2:
            continue
        for first, second in zip(points, points[1:]):
            first_node, second_node = node(first), node(second)
            if first_node != second_node:
                edges.append((first_node, second_node))
        if _is_closed_primitive(primitive, scene, tolerance):
            first_node, second_node = node(points[-1]), node(points[0])
            if first_node != second_node:
                edges.append((first_node, second_node))
    if not edges:
        return 0
    graph = _UnionFind(len(nodes))
    for first, second in edges:
        graph.union(first, second)
    component_count = len({graph.find(index) for index in range(len(nodes))})
    return max(0, len(edges) - len(nodes) + component_count)


def _is_vector_primitive(primitive: dict[str, Any]) -> bool:
    return str(primitive.get("command") or "").casefold() in {
        "line", "polyline", "arc", "circle", "ellipse",
    }


def _segments(primitive: dict[str, Any], scene: dict[str, Any]) -> list[tuple[list[float], list[float]]]:
    """Return the explicit line segments available for a primitive.

    Arcs and circles are intentionally not approximated here.  They still
    participate through their bounding boxes, while wall barriers use only
    explicit polyline/line segments.  This keeps a curved fixture edge from
    accidentally becoming an artificial wall barrier.
    """
    points = _world_points(primitive, scene)
    result = [(first, second) for first, second in zip(points, points[1:])]
    if len(points) >= 3 and _is_closed_primitive(primitive, scene, 1e-6):
        result.append((points[-1], points[0]))
    return result


def _orientation(first: list[float], second: list[float], third: list[float]) -> float:
    return ((second[0] - first[0]) * (third[1] - first[1])) - ((second[1] - first[1]) * (third[0] - first[0]))


def _on_segment(first: list[float], second: list[float], point: list[float], tolerance: float) -> bool:
    return (
        min(first[0], second[0]) - tolerance <= point[0] <= max(first[0], second[0]) + tolerance
        and min(first[1], second[1]) - tolerance <= point[1] <= max(first[1], second[1]) + tolerance
    )


def _segments_intersect(
    first_start: list[float],
    first_end: list[float],
    second_start: list[float],
    second_end: list[float],
    tolerance: float,
) -> bool:
    first_a = _orientation(first_start, first_end, second_start)
    first_b = _orientation(first_start, first_end, second_end)
    second_a = _orientation(second_start, second_end, first_start)
    second_b = _orientation(second_start, second_end, first_end)
    if ((first_a > tolerance and first_b < -tolerance) or (first_a < -tolerance and first_b > tolerance)) and (
        (second_a > tolerance and second_b < -tolerance) or (second_a < -tolerance and second_b > tolerance)
    ):
        return True
    return any((
        abs(first_a) <= tolerance and _on_segment(first_start, first_end, second_start, tolerance),
        abs(first_b) <= tolerance and _on_segment(first_start, first_end, second_end, tolerance),
        abs(second_a) <= tolerance and _on_segment(second_start, second_end, first_start, tolerance),
        abs(second_b) <= tolerance and _on_segment(second_start, second_end, first_end, tolerance),
    ))


def _bbox_center(box: list[float]) -> list[float]:
    return [(box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0]


def _point_bbox_gap(point: list[float], box: list[float]) -> float:
    return math.hypot(
        max(box[0] - point[0], point[0] - box[2], 0.0),
        max(box[1] - point[1], point[1] - box[3], 0.0),
    )


def _wall_separates(
    first_box: list[float],
    second_box: list[float],
    wall_segments: list[tuple[list[float], list[float]]],
    tolerance: float,
) -> bool:
    """Whether the straight proximity bridge crosses an explicit wall edge."""
    start, end = _bbox_center(first_box), _bbox_center(second_box)
    if _distance(start, end) <= tolerance:
        return False
    return any(_segments_intersect(start, end, wall_start, wall_end, tolerance) for wall_start, wall_end in wall_segments)


def build_isolated_fixture_regions(
    scene: dict[str, Any],
    *,
    primitives: Iterable[dict[str, Any]] | None = None,
    seed_primitive_ids: Iterable[str] = (),
    barrier_primitive_ids: Iterable[str] = (),
    snap_tolerance: float | None = None,
    max_region_span: float | None = None,
) -> list[dict[str, Any]]:
    """Find compact, wall-separated linework components for fixture recovery.

    Unlike :func:`build_closed_region_summaries`, an open component is retained
    when it contains a supplied semantic seed.  This lets a low-confidence SYP
    toilet/sink primitive expand to its *existing* neighbouring fixture
    linework.  Walls are never absorbed: they only prevent an across-wall
    proximity join and are recorded as boundary evidence.

    No line is created, inferred, or moved.  The result is therefore safe to
    hand back to a DWG Agent through its immutable primitive IDs.
    """
    primitive_list = [
        item for item in (primitives if primitives is not None else scene.get("primitives") or [])
        if isinstance(item, dict) and item.get("primitive_id")
    ]
    if not primitive_list:
        return []
    bounds = _bbox(scene.get("world_bounds"))
    scene_span = max((bounds[2] - bounds[0], bounds[3] - bounds[1], 1.0)) if bounds else 1.0
    try:
        snap = float(snap_tolerance) if snap_tolerance is not None else min(5.0, max(0.5, scene_span * 0.0001))
    except (TypeError, ValueError):
        snap = min(5.0, max(0.5, scene_span * 0.0001))
    snap = max(1e-6, snap)
    try:
        span_limit = float(max_region_span) if max_region_span is not None else max(2500.0, min(10000.0, scene_span * 0.12))
    except (TypeError, ValueError):
        span_limit = max(2500.0, min(10000.0, scene_span * 0.12))
    # CAD fixture symbols often contain deliberately tiny drafting gaps.  This
    # tolerance joins those fragments but remains far below room scale.
    join_tolerance = max(snap * 2.0, min(75.0, max(2.0, scene_span * 0.001)))
    structural_span_limit = max(2500.0, span_limit * 0.25)
    seed_ids = {str(item) for item in seed_primitive_ids if item}
    # Layer names from older/Tianzheng drawings can arrive with an incorrect
    # code page.  Callers may therefore pass high-confidence wall primitive
    # IDs from SYP as a second, independent barrier source.
    barrier_ids = {str(item) for item in barrier_primitive_ids if item}

    walls = [
        item for item in primitive_list
        if _is_wall_like(item) or str(item.get("primitive_id")) in barrier_ids
    ]
    wall_segments = [segment for wall in walls for segment in _segments(wall, scene)]
    usable: list[tuple[dict[str, Any], list[float]]] = []
    for primitive in primitive_list:
        if _is_wall_like(primitive) or str(primitive.get("primitive_id")) in barrier_ids or not _is_vector_primitive(primitive):
            continue
        box = _bbox(primitive.get("bbox_world"))
        if box is None or max(box[2] - box[0], box[3] - box[1]) > span_limit:
            continue
        # Exploding a bathroom/room parent can put wall strokes on layer 0.
        # A very long vector is structural even when it retains that broken
        # provenance, and must not act as a bridge between fixture islands.
        if max(box[2] - box[0], box[3] - box[1]) > structural_span_limit:
            continue
        usable.append((primitive, box))
    if not usable:
        return []

    union_find = _UnionFind(len(usable))
    grid_size = max(join_tolerance * 4.0, 1.0)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (_primitive, box) in enumerate(usable):
        for cell_x in range(math.floor((box[0] - join_tolerance) / grid_size), math.floor((box[2] + join_tolerance) / grid_size) + 1):
            for cell_y in range(math.floor((box[1] - join_tolerance) / grid_size), math.floor((box[3] + join_tolerance) / grid_size) + 1):
                grid[(cell_x, cell_y)].append(index)
    checked: set[tuple[int, int]] = set()
    for nearby in grid.values():
        for first in nearby:
            for second in nearby:
                if first >= second or (first, second) in checked:
                    continue
                checked.add((first, second))
                first_primitive, first_box = usable[first]
                second_primitive, second_box = usable[second]
                first_endpoints = _endpoints(first_primitive, scene)
                second_endpoints = _endpoints(second_primitive, scene)
                endpoint_touch = any(
                    _distance(first_point, second_point) <= snap
                    for first_point in first_endpoints
                    for second_point in second_endpoints
                ) or any(
                    _point_bbox_gap(point, second_box) <= snap for point in first_endpoints
                ) or any(
                    _point_bbox_gap(point, first_box) <= snap for point in second_endpoints
                )
                gap = _bbox_gap(first_box, second_box)
                # Bounding-box overlap alone is deliberately not a join: a
                # room can contain many overlapping symbol boxes.  Require an
                # actual endpoint contact, or a small positive drafting gap.
                if not endpoint_touch and not (0.0 < gap <= join_tolerance):
                    continue
                if not endpoint_touch and _wall_separates(first_box, second_box, wall_segments, snap):
                    continue
                union_find.union(first, second)

    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, (primitive, _box) in enumerate(usable):
        groups[union_find.find(index)].append(primitive)
    regions: list[dict[str, Any]] = []
    for group in groups.values():
        primitive_ids = sorted(str(item["primitive_id"]) for item in group)
        has_seed = bool(seed_ids.intersection(primitive_ids))
        closed_ids = [item_id for item_id, primitive in ((str(item["primitive_id"]), item) for item in group) if _is_closed_primitive(primitive, scene, snap)]
        cycle_rank = _cycle_rank(group, scene, snap)
        if not has_seed and not closed_ids and cycle_rank <= 0:
            continue
        bbox = _union_bbox([item.get("bbox_world") for item in group])
        if bbox is None:
            continue
        boundary_walls = [
            str(wall["primitive_id"])
            for wall in walls
            if (wall_box := _bbox(wall.get("bbox_world"))) is not None and _bbox_gap(bbox, wall_box) <= join_tolerance
        ]
        digest = hashlib.sha1("|".join(primitive_ids).encode("utf-8", errors="replace")).hexdigest()[:12]
        source = [item.get("source") or {} for item in group]
        regions.append({
            "region_id": f"isolated_fixture_{scene.get('scene_id', 'scene')}_{digest}",
            "scene_id": scene.get("scene_id"),
            "primitive_ids": primitive_ids,
            "seed_primitive_ids": sorted(seed_ids.intersection(primitive_ids)),
            "bbox_world": bbox,
            "primitive_count": len(group),
            "closed_primitive_ids": sorted(closed_ids),
            "cycle_rank": cycle_rank,
            "wall_boundary_primitive_ids": sorted(boundary_walls),
            "width_world": round(bbox[2] - bbox[0], 4),
            "height_world": round(bbox[3] - bbox[1], 4),
            "source_handles": sorted({str(item.get("handle")) for item in group if item.get("handle")}),
            "source_entity_ids": sorted({str(item.get("source_entity_id")) for item in group if item.get("source_entity_id")}),
            "layers": sorted({str(item.get("layer") or source_item.get("layer")) for item, source_item in zip(group, source) if item.get("layer") or source_item.get("layer")}),
            "block_names": sorted({str(item.get("block_name")) for item in source if item.get("block_name")}),
            "snap_tolerance_world": snap,
            "join_tolerance_world": join_tolerance,
            "closure_evidence": {
                "closed_primitive_count": len(closed_ids),
                "cycle_rank": cycle_rank,
                "semantic_seed_count": len(seed_ids.intersection(primitive_ids)),
            },
            "evidence_scope": "existing_primitives_only_wall_separated_fixture_component",
        })
    regions.sort(key=lambda item: (str(item.get("region_id")), len(item.get("primitive_ids") or [])))
    return regions


def build_closed_region_summaries(
    scene: dict[str, Any],
    *,
    primitives: Iterable[dict[str, Any]] | None = None,
    snap_tolerance: float | None = None,
    max_region_span: float | None = None,
) -> list[dict[str, Any]]:
    """Return compact closed/connected geometry regions with immutable IDs.

    The default tolerance is intentionally small relative to the scene span;
    callers may provide the known drawing-unit tolerance when the exporter has
    a documented snap precision.  Long wall-like primitives are excluded from
    connectivity, preventing an entire room boundary from becoming one
    furniture region.
    """
    primitive_list = [
        item for item in (primitives if primitives is not None else scene.get("primitives") or [])
        if isinstance(item, dict) and item.get("primitive_id")
    ]
    if not primitive_list:
        return []
    bounds = _bbox(scene.get("world_bounds"))
    scene_span = max((bounds[2] - bounds[0], bounds[3] - bounds[1], 1.0)) if bounds else 1.0
    try:
        tolerance = float(snap_tolerance) if snap_tolerance is not None else min(5.0, max(0.5, scene_span * 0.0001))
    except (TypeError, ValueError):
        tolerance = min(5.0, max(0.5, scene_span * 0.0001))
    tolerance = max(1e-6, tolerance)
    try:
        span_limit = float(max_region_span) if max_region_span is not None else max(2500.0, min(10000.0, scene_span * 0.12))
    except (TypeError, ValueError):
        span_limit = max(2500.0, min(10000.0, scene_span * 0.12))

    usable: list[tuple[int, dict[str, Any], list[float]]] = []
    for index, primitive in enumerate(primitive_list):
        command = str(primitive.get("command") or "").casefold()
        if command not in {"line", "polyline", "arc", "circle", "ellipse"}:
            continue
        box = _bbox(primitive.get("bbox_world"))
        if box is None:
            continue
        if max(box[2] - box[0], box[3] - box[1]) > span_limit:
            continue
        source = primitive.get("source") or {}
        length = float(primitive.get("length") or (primitive.get("dimensions") or {}).get("length") or 0.0)
        # Unnamed long direct entities are usually walls, axes, or dimension
        # remnants.  Excluding them keeps a room boundary from joining every
        # nearby fixture while still retaining compact block linework.
        if not source.get("block_name") and length > max(2000.0, span_limit * 0.25):
            continue
        if _is_wall_like(primitive):
            continue
        usable.append((index, primitive, box))
    if not usable:
        return []

    union_find = _UnionFind(len(usable))
    grid_size = max(tolerance * 4.0, span_limit / 128.0, 1.0)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for local_index, (_source_index, _primitive, box) in enumerate(usable):
        min_x = math.floor((box[0] - tolerance) / grid_size)
        max_x = math.floor((box[2] + tolerance) / grid_size)
        min_y = math.floor((box[1] - tolerance) / grid_size)
        max_y = math.floor((box[3] + tolerance) / grid_size)
        for cell_x in range(min_x, max_x + 1):
            for cell_y in range(min_y, max_y + 1):
                grid[(cell_x, cell_y)].append(local_index)
    checked: set[tuple[int, int]] = set()
    for nearby in grid.values():
        for first in nearby:
            for second in nearby:
                if first >= second or (first, second) in checked:
                    continue
                checked.add((first, second))
                _first_source, first_primitive, first_box = usable[first]
                _second_source, second_primitive, second_box = usable[second]
                endpoint_touch = any(
                    _distance(first_point, second_point) <= tolerance
                    for first_point in _endpoints(first_primitive, scene)
                    for second_point in _endpoints(second_primitive, scene)
                )
                if endpoint_touch or _bbox_gap(first_box, second_box) <= tolerance:
                    union_find.union(first, second)

    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for local_index, (_source_index, primitive, _box) in enumerate(usable):
        groups[union_find.find(local_index)].append(primitive)

    regions: list[dict[str, Any]] = []
    for group in groups.values():
        closed_primitive_ids = [
            str(primitive["primitive_id"])
            for primitive in group
            if _is_closed_primitive(primitive, scene, tolerance)
        ]
        cycle_rank = _cycle_rank(group, scene, tolerance)
        if not closed_primitive_ids and cycle_rank <= 0:
            continue
        primitive_ids = sorted(str(primitive["primitive_id"]) for primitive in group)
        bbox = _union_bbox([primitive.get("bbox_world") for primitive in group])
        if bbox is None:
            continue
        digest = hashlib.sha1("|".join(primitive_ids).encode("utf-8", errors="replace")).hexdigest()[:12]
        source = [primitive.get("source") or {} for primitive in group]
        regions.append({
            "region_id": f"closed_region_{scene.get('scene_id', 'scene')}_{digest}",
            "scene_id": scene.get("scene_id"),
            "primitive_ids": primitive_ids,
            "bbox_world": bbox,
            "primitive_count": len(group),
            "closed_primitive_count": len(closed_primitive_ids),
            "closed_primitive_ids": sorted(closed_primitive_ids),
            "cycle_rank": cycle_rank,
            "closed": bool(closed_primitive_ids or cycle_rank > 0),
            "area_estimate_world": round(sum(
                _polygon_area(_world_points(primitive, scene))
                for primitive in group
                if _is_closed_primitive(primitive, scene, tolerance)
            ), 4),
            "width_world": round(bbox[2] - bbox[0], 4),
            "height_world": round(bbox[3] - bbox[1], 4),
            "source_handles": sorted({str(primitive.get("handle")) for primitive in group if primitive.get("handle")}),
            "source_entity_ids": sorted({str(primitive.get("source_entity_id")) for primitive in group if primitive.get("source_entity_id")}),
            "layers": sorted({str(primitive.get("layer") or item.get("layer")) for primitive, item in zip(group, source) if primitive.get("layer") or item.get("layer")}),
            "block_names": sorted({str(item.get("block_name")) for item in source if item.get("block_name")}),
            "snap_tolerance_world": tolerance,
            "evidence_scope": "geometry_only_closed_or_cyclic_line_group",
        })
    regions.sort(key=lambda item: (str(item.get("region_id")), len(item.get("primitive_ids") or [])))
    return regions
