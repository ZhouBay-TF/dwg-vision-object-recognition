from __future__ import annotations

"""Deterministic view construction for SymPointV2.

The drawing frame is only a routing boundary.  SymPointV2 receives a view
whose bounds are derived from effective geometry inside that frame.  Keeping
this logic separate from CAD extraction makes it possible to run controlled
A/B experiments without changing world coordinates or Scene Graph output.
"""

import math
from typing import Any, Iterable

from .contracts import require_bbox


DEFAULT_TRAINING_CANVAS_SIDE = 140.0
DEFAULT_SINGLE_DOOR_TARGET_LONG_EDGE_PAPER = 9.0
DEFAULT_SINGLE_DOOR_TARGET_SHORT_EDGE_PAPER = 8.0
DEFAULT_SINGLE_DOOR_REFERENCE_LONG_WORLD = 900.0
DEFAULT_SINGLE_DOOR_REFERENCE_SCALE_DENOMINATOR = 100.0
DEFAULT_SINGLE_DOOR_TARGET_WORLD_PER_TRAINING_PAPER_UNIT = (
    DEFAULT_SINGLE_DOOR_REFERENCE_LONG_WORLD / DEFAULT_SINGLE_DOOR_TARGET_LONG_EDGE_PAPER
)
DEFAULT_SINGLE_DOOR_LONG_EDGE_RATIO = (
    DEFAULT_SINGLE_DOOR_TARGET_LONG_EDGE_PAPER / DEFAULT_TRAINING_CANVAS_SIDE
)
DEFAULT_SINGLE_DOOR_SCALE_STATS: dict[str, Any] = {
    "semantic_id": 0,
    "class_name": "single door",
    "source": "SymPointV2 validation set (dataset/svg/val/*_s2.json) with current DWG reference",
    "quality": "complete_val_non_degenerate_bboxes",
    "train_cache": {
        "valid_files": 1293,
        "total_files": 3556,
        "instances": 781,
        "long_edge_mean_world": 8.999791,
        "long_edge_median_world": 9.5,
        "long_edge_mean_ratio": 0.064294,
        "long_edge_median_ratio": 0.067857,
    },
    "val_cross_check": {
        "files": 810,
        "files_with_single_door": 431,
        "raw_boxes": 2334,
        "valid_boxes": 2296,
        "degenerate_boxes_excluded": 38,
        "instances": 2296,
        "long_edge_mean_world": 8.755064,
        "long_edge_median_world": 9.0,
        "long_edge_mean_ratio": 0.062548,
        "long_edge_median_ratio": 0.064286,
        "bbox_width_p25_p50_p75": [6.323136, 8.73, 10.0],
        "bbox_height_p25_p50_p75": [6.656975, 8.774, 9.75],
        "long_edge_p25_p50_p75": [7.31275, 9.0, 10.0],
        "short_edge_p25_p50_p75": [5.51625, 8.0, 9.75],
        "area_p25_p50_p75": [35.689105, 70.146, 97.5],
    },
    "target_long_edge_ratio": DEFAULT_SINGLE_DOOR_LONG_EDGE_RATIO,
    "target_long_edge_paper": DEFAULT_SINGLE_DOOR_TARGET_LONG_EDGE_PAPER,
    "target_short_edge_paper": DEFAULT_SINGLE_DOOR_TARGET_SHORT_EDGE_PAPER,
    "current_dwg": {
        "filename": "平面深化简化.dwg",
        "block_family": "$DorLib2D$00000001",
        "references": 26,
        "long_edge_world_min_median_max": [800.0, 900.0, 1000.0],
        "short_edge_world_min_median_max": [780.0, 877.5, 975.0],
        "paper_scale": "1:100",
        "long_edge_paper_min_median_max": [8.0, 9.0, 10.0],
    },
    "current_dwg_reference_long_world": DEFAULT_SINGLE_DOOR_REFERENCE_LONG_WORLD,
    "current_dwg_reference_scale_denominator": DEFAULT_SINGLE_DOOR_REFERENCE_SCALE_DENOMINATOR,
}


def calculate_scale_calibration(
    scale_evidence: dict[str, Any] | None,
    *,
    training_canvas_side: float = DEFAULT_TRAINING_CANVAS_SIDE,
    reference_long_world: float | None = None,
    target_long_edge_paper: float = DEFAULT_SINGLE_DOOR_TARGET_LONG_EDGE_PAPER,
) -> dict[str, Any]:
    """Calculate the scale-only SymPoint tile coefficient.

    The validation-set median single-door long edge is 9 paper units on the
    model's approximately 140-unit canvas.  The current DWG calibration
    sample has a 900-world-unit canonical single door at 1:100, so the target
    is 100 world units per training paper unit.  For a drawing scale 1:N,
    ``scale_coefficient = 100 / N`` is applied to the raw ``140 * N`` tile
    side.  This keeps the door's paper-space size stable across drawing
    scales while preserving native world coordinates for result fusion.
    """
    evidence = scale_evidence or {}
    world_per_paper_unit = float(evidence.get("world_units_per_paper_unit_same_unit") or 0.0)
    paper_scale = float(evidence.get("paper_units_per_world_unit_same_unit") or 0.0)
    reference_world = float(
        reference_long_world
        if reference_long_world is not None
        else DEFAULT_SINGLE_DOOR_REFERENCE_LONG_WORLD
    )
    target_long = float(target_long_edge_paper)
    usable = bool(evidence.get("usable")) and world_per_paper_unit > 0.0 and paper_scale > 0.0
    if not usable or reference_world <= 0.0 or target_long <= 0.0:
        return {
            "status": "unavailable",
            "usable": False,
            "formula": "target_world_per_training_paper_unit / scale_denominator",
            "scale_coefficient": None,
            "world_units_per_paper_unit": world_per_paper_unit or None,
            "paper_units_per_world_unit": paper_scale or None,
            "reference_long_world": reference_world,
            "target_long_edge_paper": target_long,
            "target_world_per_training_paper_unit": None,
            "raw_training_canvas_side_world": None,
            "training_canvas_side_world": None,
        }

    target_world_per_training_paper_unit = reference_world / target_long
    scale_coefficient = target_world_per_training_paper_unit / world_per_paper_unit
    raw_side_world = float(training_canvas_side) * world_per_paper_unit
    calibrated_side_world = raw_side_world * scale_coefficient
    return {
        "status": "applied",
        "usable": True,
        "formula": "(reference_long_world * paper_units_per_world_unit) / target_long_edge_paper",
        "scale_coefficient": scale_coefficient,
        "world_units_per_paper_unit": world_per_paper_unit,
        "paper_units_per_world_unit": paper_scale,
        "scale_denominator": world_per_paper_unit,
        "reference_long_world": reference_world,
        "reference_long_edge_paper": reference_world * paper_scale,
        "target_long_edge_paper": target_long,
        "target_world_per_training_paper_unit": target_world_per_training_paper_unit,
        "raw_training_canvas_side_world": raw_side_world,
        "training_canvas_side_world": calibrated_side_world,
        "training_canvas_side_paper": float(training_canvas_side),
        "reference_source": (
            "explicit_reference_long_world"
            if reference_long_world is not None
            else "current_dwg_single_door_mbr_median"
        ),
    }


AUXILIARY_LAYER_TERMS = (
    "axis",
    "grid",
    "dimension",
    "dim_",
    "anno",
    "annotation",
    "border",
    "frame",
    "title",
    "viewport",
    "print",
    "打印",
    "defpoints",
    "图框",
    "图签",
    "轴网",
    "轴线",
    "标注",
    "尺寸",
)


def _bbox_union(boxes: Iterable[Iterable[float]]) -> list[float]:
    values = [require_bbox(box, "geometry bbox") for box in boxes]
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(box[0] for box in values),
        min(box[1] for box in values),
        max(box[2] for box in values),
        max(box[3] for box in values),
    ]


def _expand(box: list[float], ratio: float) -> list[float]:
    width = max(0.0, box[2] - box[0])
    height = max(0.0, box[3] - box[1])
    dx = max(width, height) * max(0.0, float(ratio))
    return [box[0] - dx, box[1] - dx, box[2] + dx, box[3] + dx]


def _square(box: list[float], padding_ratio: float = 0.0) -> list[float]:
    expanded = _expand(box, padding_ratio)
    width = max(0.0, expanded[2] - expanded[0])
    height = max(0.0, expanded[3] - expanded[1])
    side = max(width, height)
    if side <= 0.0:
        raise ValueError("有效几何区域的宽高不能同时为零")
    center_x = (expanded[0] + expanded[2]) / 2.0
    center_y = (expanded[1] + expanded[3]) / 2.0
    return [center_x - side / 2.0, center_y - side / 2.0, center_x + side / 2.0, center_y + side / 2.0]


def _intersects(first: list[float], second: list[float]) -> bool:
    return not (first[2] < second[0] or second[2] < first[0] or first[3] < second[1] or second[3] < first[1])


def _is_auxiliary_layer(layer: Any) -> bool:
    value = str(layer or "").strip().lower()
    return bool(value) and any(term in value for term in AUXILIARY_LAYER_TERMS)


def _is_scale_primitive(primitive: dict[str, Any], scale_evidence: dict[str, Any] | None) -> bool:
    if not scale_evidence or not scale_evidence.get("usable"):
        return False
    exclusion = scale_evidence.get("exclusion_bbox_world")
    if not exclusion:
        return False
    primitive_box = require_bbox(primitive.get("bbox_world") or primitive.get("bbox_local"), "primitive bbox")
    scale_box = require_bbox(exclusion, "scale exclusion bbox")
    if not _intersects(primitive_box, scale_box):
        return False
    # Prefer the native layer as a guard.  A title-block scale bar may sit
    # near a wall or border; a broad spatial exclusion must not remove that
    # neighboring architecture from the effective Scene.
    scale_layer = str(scale_evidence.get("layer") or "").strip().lower()
    primitive_layer = str(primitive.get("layer") or "").strip().lower()
    return bool(scale_layer) and primitive_layer == scale_layer


def effective_bbox_world(
    scene: dict[str, Any],
    *,
    exclude_auxiliary: bool = True,
    scale_evidence: dict[str, Any] | None = None,
    constrain_to_scene_bounds: bool = True,
) -> tuple[list[float], list[str]]:
    """Return the bbox of usable geometry, independent of the frame bbox.

    Frame-like primitives are always excluded.  Auxiliary-layer exclusion is
    conservative and recorded in the returned layer list so the decision is
    auditable; it does not remove primitives from ``scene.json`` or alter
    world-coordinate reconciliation.
    """
    boxes: list[list[float]] = []
    excluded_layers: set[str] = set()
    fallback_boxes: list[list[float]] = []
    all_boxes: list[list[float]] = []
    scale_exclusion = None
    if scale_evidence and scale_evidence.get("usable"):
        exclusion = scale_evidence.get("exclusion_bbox_world")
        if exclusion:
            scale_exclusion = require_bbox(exclusion, "scale exclusion bbox")
    for primitive in scene.get("primitives") or []:
        if not isinstance(primitive, dict) or primitive.get("is_frame"):
            continue
        box = primitive.get("bbox_world") or primitive.get("bbox_local")
        if not box:
            continue
        normalized = require_bbox(box, "primitive bbox")
        all_boxes.append(normalized)
        if scale_exclusion and _is_scale_primitive(primitive, scale_evidence):
            continue
        fallback_boxes.append(normalized)
        layer = str(primitive.get("layer") or "")
        if exclude_auxiliary and _is_auxiliary_layer(layer):
            excluded_layers.add(layer)
            continue
        boxes.append(normalized)
    if not boxes:
        boxes = fallback_boxes or all_boxes
        excluded_layers = set()
    if not boxes:
        raise ValueError("Scene 没有可计算有效区域的几何图元")
    effective = _bbox_union(boxes)
    if constrain_to_scene_bounds:
        # A content Scene can intentionally exclude the title block while a
        # long axis/dimension primitive still crosses its boundary.  Its raw
        # bbox must not expand the model viewport back to the entire sheet.
        scene_bounds = require_bbox(scene.get("world_bounds"), "scene.world_bounds")
        clipped = [
            max(effective[0], scene_bounds[0]),
            max(effective[1], scene_bounds[1]),
            min(effective[2], scene_bounds[2]),
            min(effective[3], scene_bounds[3]),
        ]
        if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
            effective = clipped
    return effective, sorted(excluded_layers)


def _centered_square(center: tuple[float, float], side: float) -> list[float]:
    side = max(float(side), 1e-9)
    half = side / 2.0
    return [center[0] - half, center[1] - half, center[0] + half, center[1] + half]


def build_sympoint_view(
    scene: dict[str, Any],
    *,
    mode: str = "effective_square",
    padding_ratio: float = 0.0,
    exclude_auxiliary: bool = True,
    reference_long_world: float | None = None,
    target_long_edge_ratio: float = DEFAULT_SINGLE_DOOR_LONG_EDGE_RATIO,
    scale_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic model view and its provenance.

    Modes:

    ``frame`` keeps the legacy frame view for baseline comparison.
    ``effective_rect`` uses the effective-geometry rectangle.
    ``effective_square`` pads that rectangle to a square by its longest edge.
    ``calibrated_square`` chooses a square side so a supplied reliable
    reference object's longest edge matches the training single-door ratio.

    Calibration is intentionally not guessed.  When no reliable door anchor
    is supplied, the calibrated mode falls back to ``effective_square`` and
    records the missing anchor in the result.
    """
    if mode not in {"frame", "effective_rect", "effective_square", "calibrated_square", "scale_calibrated_square"}:
        raise ValueError(f"unsupported SymPointV2 view mode: {mode}")
    effective, excluded_layers = effective_bbox_world(
        scene,
        exclude_auxiliary=exclude_auxiliary,
        scale_evidence=scale_evidence,
    )
    frame_bounds = require_bbox(scene.get("world_bounds"), "scene.world_bounds")
    effective_square = _square(effective, padding_ratio)
    effective_width = max(0.0, effective[2] - effective[0])
    effective_height = max(0.0, effective[3] - effective[1])
    effective_side = max(effective_width, effective_height)
    calibration_status = "not_requested"
    calibration_factor = 1.0
    reference_ratio: float | None = None
    scale_evidence = scale_evidence or {}
    scale_usable = bool(scale_evidence.get("usable"))
    paper_scale = float(scale_evidence.get("paper_units_per_world_unit_same_unit") or 0.0) if scale_usable else 0.0
    world_per_paper_unit = float(scale_evidence.get("world_units_per_paper_unit_same_unit") or 0.0) if scale_usable else 0.0
    scale_projection_status = "not_available"
    scale_target_side_world: float | None = None
    scale_calibration = calculate_scale_calibration(
        scale_evidence,
        reference_long_world=reference_long_world,
    )

    if mode == "frame":
        view_bounds = frame_bounds
    elif mode == "effective_rect":
        view_bounds = _expand(effective, padding_ratio)
    elif mode == "effective_square":
        view_bounds = effective_square
    elif mode == "calibrated_square":
        reference = float(reference_long_world or 0.0)
        target = float(target_long_edge_ratio)
        if reference > 0.0 and target > 0.0:
            target_side = reference / target
            center = ((effective[0] + effective[2]) / 2.0, (effective[1] + effective[3]) / 2.0)
            view_bounds = _centered_square(center, target_side)
            calibration_factor = effective_side / max(target_side, 1e-9)
            reference_ratio = reference / max(target_side, 1e-9)
            calibration_status = "applied"
        else:
            view_bounds = effective_square
            calibration_status = "missing_scale_anchor_fallback_to_effective_square"
    else:
        if scale_usable and world_per_paper_unit > 0.0:
            scale_target_side_world = scale_calibration.get("training_canvas_side_world")
            if not scale_target_side_world:
                scale_target_side_world = DEFAULT_TRAINING_CANVAS_SIDE * world_per_paper_unit
            center = ((effective[0] + effective[2]) / 2.0, (effective[1] + effective[3]) / 2.0)
            target_square = _centered_square(center, scale_target_side_world)
            if effective_side <= scale_target_side_world + 1e-7:
                view_bounds = target_square
                scale_projection_status = "applied"
                calibration_status = "scale_1_to_training_canvas"
            else:
                # Never silently discard valid geometry.  A large frame must
                # use scale tiles; this single-view fallback keeps the whole
                # effective region available for the legacy one-call path.
                view_bounds = effective_square
                scale_projection_status = "requires_scale_tiles"
                calibration_status = "scale_target_smaller_than_effective_region_fallback_to_effective_square"
        else:
            view_bounds = effective_square
            scale_projection_status = "missing_scale_fallback"
            calibration_status = "missing_scale_fallback_to_effective_square"

    view_width = max(1e-9, view_bounds[2] - view_bounds[0])
    view_height = max(1e-9, view_bounds[3] - view_bounds[1])
    effective_fits = (
        view_bounds[0] <= effective[0] + 1e-7
        and view_bounds[1] <= effective[1] + 1e-7
        and view_bounds[2] >= effective[2] - 1e-7
        and view_bounds[3] >= effective[3] - 1e-7
    )
    effective_primitive_ids = [
        str(primitive.get("primitive_id") or "")
        for primitive in scene.get("primitives") or []
        if isinstance(primitive, dict)
        and not primitive.get("is_frame")
        and not (
            scale_evidence
            and scale_evidence.get("usable")
            and scale_evidence.get("exclusion_bbox_world")
            and _is_scale_primitive(primitive, scale_evidence)
        )
        and not (exclude_auxiliary and _is_auxiliary_layer(primitive.get("layer")))
        and _intersects(require_bbox(primitive.get("bbox_world") or primitive.get("bbox_local")), effective)
    ]
    return {
        "schema_version": "sympoint_view.v1",
        "mode": mode,
        "effective_bbox_world": effective,
        "effective_square_world": effective_square,
        "view_bounds_world": view_bounds,
        "view_width_world": view_width,
        "view_height_world": view_height,
        "effective_side_world": effective_side,
        "training_normalization": {
            "reference_canvas_width": DEFAULT_TRAINING_CANVAS_SIDE,
            "reference_canvas_height": DEFAULT_TRAINING_CANVAS_SIDE,
            "world_units_per_reference_canvas_x": view_width / DEFAULT_TRAINING_CANVAS_SIDE,
            "world_units_per_reference_canvas_y": view_height / DEFAULT_TRAINING_CANVAS_SIDE,
            "reference_canvas_x_per_world_unit": DEFAULT_TRAINING_CANVAS_SIDE / view_width,
            "reference_canvas_y_per_world_unit": DEFAULT_TRAINING_CANVAS_SIDE / view_height,
            "svg_display_scale": paper_scale if paper_scale > 0.0 else 1.0,
            "paper_view_width": view_width * (paper_scale if paper_scale > 0.0 else 1.0),
            "paper_view_height": view_height * (paper_scale if paper_scale > 0.0 else 1.0),
            "scale_projection_status": scale_projection_status,
            "target_training_canvas_side_world": scale_target_side_world,
            "scale_calibration": scale_calibration,
            "scale_correction_coefficient": scale_calibration.get("scale_coefficient"),
            "note": "SVG 使用纸面单位时，坐标和 viewBox 同时乘 paper_units_per_world_unit；SymPointV2 仍按 viewBox 归一化，尺度用于训练分布裁剪与审计，不改变回映射世界坐标。",
        },
        "effective_primitive_count": len(effective_primitive_ids),
        "effective_primitive_ids": effective_primitive_ids,
        "excluded_auxiliary_layers": excluded_layers,
        "scale_evidence": scale_evidence,
        "effective_geometry_fits_view": effective_fits,
        "calibration": {
            "status": calibration_status,
            "target_class": "single door",
            "target_long_edge_ratio": float(target_long_edge_ratio),
            "reference_long_world": reference_long_world,
            "reference_long_edge_ratio": reference_ratio,
            "zoom_factor_relative_to_effective_square": calibration_factor,
            "source_stats": DEFAULT_SINGLE_DOOR_SCALE_STATS,
            "scale_projection_status": scale_projection_status,
            "paper_units_per_world_unit": paper_scale if paper_scale > 0.0 else None,
            "world_units_per_paper_unit": world_per_paper_unit if world_per_paper_unit > 0.0 else None,
            "target_training_canvas_side_world": scale_target_side_world,
            "scale_calibration": scale_calibration,
        },
        "coordinate_transform": {
            "world_origin": [view_bounds[0], view_bounds[1]],
            "scale": 1.0,
            "svg_scale": paper_scale if paper_scale > 0.0 else 1.0,
            "rotation_deg": 0.0,
        },
    }


def _axis_starts(lower: float, upper: float, side: float, overlap_ratio: float) -> list[float]:
    """Return deterministic starts that cover an axis with overlap."""
    span = max(0.0, float(upper) - float(lower))
    side = max(1e-9, float(side))
    if span <= side:
        return [float(lower + (span - side) / 2.0)]
    stride = side * max(0.05, 1.0 - min(0.90, max(0.0, float(overlap_ratio))))
    starts = [float(lower)]
    while starts[-1] + side < upper - 1e-7:
        next_start = starts[-1] + stride
        if next_start + side >= upper - 1e-7:
            next_start = float(upper - side)
        if next_start <= starts[-1] + 1e-7:
            break
        starts.append(next_start)
    return starts


def build_scale_tile_views(
    scene: dict[str, Any],
    *,
    scale_evidence: dict[str, Any] | None = None,
    training_canvas_side: float = DEFAULT_TRAINING_CANVAS_SIDE,
    overlap_ratio: float = 0.10,
    exclude_auxiliary: bool = True,
    reference_long_world: float | None = None,
) -> list[dict[str, Any]]:
    """Split a large effective Scene into training-scale square model views.

    The tile size is computed in DWG world units from the native paper scale
    and the single-door MBR calibration.  For the current 1:100 reference,
    the coefficient is 1.0 and the tile remains 14,000 world units.  Each
    view keeps world bounds and original primitive IDs so the caller can merge
    model results without lossy raster-coordinate guesses.
    """
    evidence = scale_evidence or scene.get("scale_evidence") or {}
    if evidence.get("status") != "detected" or not evidence.get("usable"):
        return []
    world_per_paper_unit = float(evidence.get("world_units_per_paper_unit_same_unit") or 0.0)
    paper_scale = float(evidence.get("paper_units_per_world_unit_same_unit") or 0.0)
    if world_per_paper_unit <= 0.0 or paper_scale <= 0.0:
        return []
    scale_calibration = calculate_scale_calibration(
        evidence,
        training_canvas_side=training_canvas_side,
        reference_long_world=reference_long_world,
    )
    effective, excluded_layers = effective_bbox_world(
        scene,
        exclude_auxiliary=exclude_auxiliary,
        scale_evidence=evidence,
    )
    side = max(
        1e-9,
        float(scale_calibration.get("training_canvas_side_world") or 0.0),
    )
    if side <= 1e-9:
        side = max(1e-9, float(training_canvas_side) * world_per_paper_unit)
    xs = _axis_starts(effective[0], effective[2], side, overlap_ratio)
    ys = _axis_starts(effective[1], effective[3], side, overlap_ratio)
    views: list[dict[str, Any]] = []
    for row, y in enumerate(ys):
        for col, x in enumerate(xs):
            bounds = [x, y, x + side, y + side]
            views.append({
                "schema_version": "sympoint_view.v1",
                "mode": "scale_tile",
                "parent_scene_id": str(scene.get("scene_id") or ""),
                "tile_index": len(views),
                "tile_grid": {"row": row, "column": col, "rows": len(ys), "columns": len(xs)},
                "effective_bbox_world": effective,
                "effective_square_world": bounds,
                "view_bounds_world": bounds,
                "view_width_world": side,
                "view_height_world": side,
                "effective_side_world": max(effective[2] - effective[0], effective[3] - effective[1]),
                "effective_primitive_count": 0,
                "effective_primitive_ids": [],
                "excluded_auxiliary_layers": excluded_layers,
                "scale_evidence": evidence,
                "effective_geometry_fits_view": False,
                "calibration": {
                    "status": "scale_tile",
                    "target_class": "single door",
                    "target_long_edge_ratio": DEFAULT_SINGLE_DOOR_LONG_EDGE_RATIO,
                    "paper_canvas_side": float(training_canvas_side),
                    "paper_units_per_world_unit": paper_scale,
                    "world_units_per_paper_unit": world_per_paper_unit,
                    "scale_coefficient": scale_calibration.get("scale_coefficient"),
                    "target_world_per_training_paper_unit": scale_calibration.get(
                        "target_world_per_training_paper_unit"
                    ),
                    "overlap_ratio": float(overlap_ratio),
                    "scale_calibration": scale_calibration,
                },
                "training_normalization": {
                    "reference_canvas_width": float(training_canvas_side),
                    "reference_canvas_height": float(training_canvas_side),
                    "svg_display_scale": paper_scale,
                    "paper_view_width": float(training_canvas_side),
                    "paper_view_height": float(training_canvas_side),
                    "scale_projection_status": "applied",
                    "target_training_canvas_side_world": side,
                    "scale_correction_coefficient": scale_calibration.get("scale_coefficient"),
                    "note": "scale tile uses native paper scale and the training-equivalent square canvas.",
                },
                "coordinate_transform": {
                    "world_origin": [x, y],
                    "scale": 1.0,
                    "svg_scale": paper_scale,
                    "rotation_deg": 0.0,
                },
            })
    return views
