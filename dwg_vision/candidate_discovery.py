from __future__ import annotations

"""Open-world candidate discovery for CAD/SymPoint reconciliation.

SymPointV2 is a useful closed-set detector, but its primitive mask can attach
an equipment symbol to a neighbouring object (for example, toilet linework to
the bathroom door).  This module keeps those weak signals as auditable
candidates instead of forcing them into the existing Scene Graph objects.

The module never invents geometry.  Every candidate is a deterministic union
of primitive IDs already present in the Scene Bundle, with optional text and
SYP evidence attached to it.
"""

import hashlib
import math
import re
from collections import defaultdict
from typing import Any, Iterable

from .closed_regions import build_closed_region_summaries, build_isolated_fixture_regions


# Thing classes are intentionally kept open here.  The final Agent decides
# whether a candidate is a real object; this layer only says that the geometry
# deserves semantic verification.
_SYP_THING_IDS = set(range(10, 30))
_SYP_CLASS_NAMES = {
    0: "single_door",
    1: "double_door",
    2: "sliding_door",
    3: "folding_door",
    4: "revolving_door",
    5: "rolling_door",
    6: "window",
    10: "sofa",
    11: "bed",
    12: "chair",
    13: "table",
    14: "tv_cabinet",
    15: "wardrobe",
    16: "cabinet",
    17: "gas_stove",
    18: "sink",
    19: "refrigerator",
    20: "airconditioner",
    21: "bath",
    22: "bath_tub",
    23: "washing_machine",
    24: "squat_toilet",
    25: "urinal",
    26: "toilet",
    27: "stairs",
    28: "elevator",
    29: "escalator",
}
_EQUIPMENT_TERMS = (
    "sofa", "bed", "chair", "table", "cabinet", "wardrobe", "sink",
    "toilet", "bath", "tub", "stove", "refrigerator", "washing",
    "urinal", "airconditioner", "家具", "洁具", "马桶", "洗手盆",
    "淋浴", "浴缸",
)
_DOOR_TERMS = ("door", "门")
_WINDOW_TERMS = ("window", "窗", "opening")
_ROOM_TERMS = {
    "卫生间": ("bathroom", ("toilet", "squat_toilet", "urinal", "sink", "bath_tub", "shower", "washing_machine")),
    "厨房": ("kitchen", ("sink", "gas_stove", "refrigerator", "cabinet")),
    "主卧室": ("bedroom", ("bed", "wardrobe", "cabinet")),
    "卧室": ("bedroom", ("bed", "wardrobe", "cabinet")),
    "客厅": ("living_room", ("sofa", "table", "chair", "tv_cabinet")),
    "餐厅": ("dining_room", ("table", "chair", "cabinet")),
    "阳台": ("balcony", ("washing_machine", "cabinet", "railing")),
}


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result):
        return None
    return [min(result[0], result[2]), min(result[1], result[3]), max(result[0], result[2]), max(result[1], result[3])]


def _union(boxes: Iterable[list[float]]) -> list[float] | None:
    values = [item for box in boxes if (item := _bbox(box)) is not None]
    if not values:
        return None
    return [
        min(item[0] for item in values),
        min(item[1] for item in values),
        max(item[2] for item in values),
        max(item[3] for item in values),
    ]


def _class_name(value: dict[str, Any]) -> str:
    explicit = str(value.get("class_name") or value.get("class") or "").strip()
    if explicit:
        return explicit
    try:
        return _SYP_CLASS_NAMES.get(int(value.get("class_id")), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.casefold()).strip("_") or "unknown"


def _subtype(value: str) -> str:
    text = str(value or "").strip().casefold()
    terms: tuple[tuple[tuple[str, ...], str], ...] = (
        (("壁挂坐便", "壁挂马桶", "wall hung toilet", "wall-hung toilet"), "wall_hung_toilet"),
        (("坐便器", "坐便", "马桶", "toilet", "wc"), "toilet"),
        (("蹲便", "蹲厕", "squat toilet"), "squat_toilet"),
        (("小便器", "小便斗", "urinal"), "urinal"),
        (("洗手盆", "洗脸盆", "台盆", "sink", "basin"), "sink"),
        (("浴缸", "bathtub", "bath tub"), "bath_tub"),
        (("淋浴", "shower"), "shower"),
    )
    for candidates, subtype in terms:
        if any(term in text for term in candidates):
            return subtype
    return _slug(text)


def _type_for_subtype(subtype: str) -> str:
    text = str(subtype or "").casefold()
    if any(term in text for term in _DOOR_TERMS):
        return "door"
    if any(term in text for term in _WINDOW_TERMS):
        return "window"
    if any(term in text for term in _EQUIPMENT_TERMS):
        return "furniture"
    return "furniture"


def _primitive_ids(value: dict[str, Any], scene: dict[str, Any]) -> list[str]:
    ids = [str(item) for item in value.get("primitive_ids") or [] if item]
    if ids:
        return ids
    if value.get("primitive_id"):
        return [str(value["primitive_id"])]
    result: list[str] = []
    primitives = scene.get("primitives") or []
    indices = list(value.get("primitive_indices") or [])
    if value.get("primitive_index") is not None:
        indices.append(value.get("primitive_index"))
    for index in indices:
        try:
            primitive = primitives[int(index)]
        except (IndexError, TypeError, ValueError):
            continue
        if isinstance(primitive, dict) and primitive.get("primitive_id"):
            result.append(str(primitive["primitive_id"]))
    return result


def _component_key(primitive: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    source = primitive.get("source") or {}
    handle = str(source.get("handle") or primitive.get("handle") or "")
    block_name = str(source.get("block_name") or "").strip()
    parent = str(source.get("exploded_from") or "").strip()
    if block_name and parent:
        return (
            f"leaf:{handle}|{block_name}|{parent}",
            {"native_root_handle": handle, "block_name": block_name, "parent_path": parent},
        )
    # A direct native entity without block provenance is kept as a singleton.
    entity_id = str(primitive.get("source_entity_id") or primitive.get("primitive_id") or "")
    return (f"primitive:{entity_id}", {"native_root_handle": handle, "block_name": "", "parent_path": ""})


def _nearby_texts(box: list[float], texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    scale = max(1.0, min(box[2] - box[0], box[3] - box[1]))
    max_distance = max(500.0, scale * 3.0)
    result: list[dict[str, Any]] = []
    for text in texts:
        if str(text.get("role") or "") == "dimension":
            continue
        text_box = _bbox(text.get("bbox_world"))
        if text_box is None:
            continue
        text_center = ((text_box[0] + text_box[2]) / 2.0, (text_box[1] + text_box[3]) / 2.0)
        distance = math.dist(center, text_center)
        if distance <= max_distance or text.get("role") == "equipment_label":
            result.append({
                "text_id": text.get("text_id") or text.get("handle"),
                "text": str(text.get("normalized_text") or text.get("text") or ""),
                "role": text.get("role", ""),
                "bbox_world": text_box,
                "distance_world": distance,
            })
    return sorted(result, key=lambda item: float(item.get("distance_world", 0.0)))[:16]


def _room_context(box: list[float], texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
    span = max(abs(box[2] - box[0]), abs(box[3] - box[1]), 1.0)
    result: list[dict[str, Any]] = []
    for text in texts:
        if str(text.get("role") or "") != "room_label":
            continue
        text_box = _bbox(text.get("bbox_world"))
        if text_box is None:
            continue
        text_center = ((text_box[0] + text_box[2]) / 2.0, (text_box[1] + text_box[3]) / 2.0)
        distance = math.dist(center, text_center)
        if distance > max(1200.0, span * 8.0):
            continue
        label = str(text.get("normalized_text") or text.get("text") or "")
        room_type, likely = next(
            ((room_type, likely) for name, (room_type, likely) in _ROOM_TERMS.items() if name in label),
            ("unknown_room", ()),
        )
        result.append({
            "text_id": text.get("text_id") or text.get("handle"),
            "label": label,
            "room_type": room_type,
            "likely_subtypes": list(likely),
            "bbox_world": text_box,
            "distance_world": distance,
        })
    return sorted(result, key=lambda item: float(item.get("distance_world", 0.0)))[:3]


def _existing_labels(
    primitive_ids: set[str],
    *,
    native_candidates: list[dict[str, Any]],
    sympoint_result: dict[str, Any],
    scene: dict[str, Any],
) -> set[str]:
    labels: set[str] = set()
    for item in native_candidates:
        if primitive_ids.intersection({str(value) for value in item.get("primitive_ids") or []}):
            labels.add(_subtype(str(item.get("subtype") or item.get("type") or "unknown")))
    for instance in sympoint_result.get("instances") or []:
        if not primitive_ids.intersection(set(_primitive_ids(instance, scene))):
            continue
        labels.add(_subtype(_class_name(instance)))
    return {item for item in labels if item and item != "unknown"}


def _component_geometry(primitives: list[dict[str, Any]]) -> dict[str, Any]:
    boxes = [_bbox(item.get("bbox_world")) for item in primitives]
    boxes = [item for item in boxes if item is not None]
    bbox = _union(boxes)
    commands: dict[str, int] = defaultdict(int)
    layers: dict[str, int] = defaultdict(int)
    lengths: list[float] = []
    for primitive in primitives:
        commands[str(primitive.get("command") or primitive.get("subtype") or "unknown")] += 1
        layers[str(primitive.get("layer") or (primitive.get("source") or {}).get("layer") or "")] += 1
        try:
            length = float(primitive.get("length") or (primitive.get("dimensions") or {}).get("length") or 0.0)
        except (TypeError, ValueError):
            length = 0.0
        if length > 0.0:
            lengths.append(length)
    return {
        "primitive_count": len(primitives),
        "command_counts": dict(sorted(commands.items())),
        "layer_counts": dict(sorted(layers.items())),
        "length_min_world": min(lengths) if lengths else 0.0,
        "length_median_world": sorted(lengths)[len(lengths) // 2] if lengths else 0.0,
        "length_max_world": max(lengths) if lengths else 0.0,
        "bbox_world": bbox,
        "width_world": (bbox[2] - bbox[0]) if bbox else 0.0,
        "height_world": (bbox[3] - bbox[1]) if bbox else 0.0,
    }


def _semantic_hypotheses(signals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse primitive-level SYP scores into auditable subtype hypotheses."""
    signal_by_subtype: dict[str, dict[str, Any]] = {}
    for signal in signals:
        subtype = str(signal.get("subtype") or "unknown")
        current = signal_by_subtype.get(subtype)
        if current is None:
            current = {
                "subtype": subtype,
                "class_name": signal.get("class_name", "unknown"),
                "class_id": signal.get("class_id", -1),
                "max_score": float(signal.get("score", 0.0) or 0.0),
                "primitive_count": 0,
                "primitive_ids": [],
            }
            signal_by_subtype[subtype] = current
        current["max_score"] = max(float(current["max_score"]), float(signal.get("score", 0.0) or 0.0))
        current["primitive_count"] += 1
        primitive_id = str(signal.get("primitive_id") or "")
        if primitive_id and primitive_id not in current["primitive_ids"]:
            current["primitive_ids"].append(primitive_id)
    return sorted(
        signal_by_subtype.values(),
        key=lambda item: (float(item.get("max_score", 0.0)), int(item.get("primitive_count", 0))),
        reverse=True,
    )[:8]


def build_open_world_candidates(
    scene: dict[str, Any],
    sympoint_result: dict[str, Any],
    *,
    native_candidates: Iterable[dict[str, Any]] = (),
    texts: Iterable[dict[str, Any]] | None = None,
    min_semantic_score: float | None = None,
    include_closed_region_candidates: bool = True,
    max_closed_region_candidates: int = 96,
) -> list[dict[str, Any]]:
    """Build bounded semantic-gap candidates from existing Scene geometry.

    A candidate is emitted when a compact leaf block/component has either a
    SYP thing-class signal or a nearby direct equipment label.  Existing
    agreement is suppressed; disagreement and missing-object signals remain
    available to the final Agent.  The default threshold deliberately reaches
    the roughly-uniform 1/35 SYP scores, because a weak semantic argmax is a
    recall signal here, never an object confidence.
    """
    try:
        threshold = float(min_semantic_score if min_semantic_score is not None else 0.02)
    except (TypeError, ValueError):
        threshold = 0.02
    threshold = max(0.0, min(1.0, threshold))
    primitive_list = [item for item in scene.get("primitives") or [] if isinstance(item, dict) and item.get("primitive_id")]
    primitive_lookup = {str(item["primitive_id"]): item for item in primitive_list}
    component_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    component_meta: dict[str, dict[str, Any]] = {}
    for primitive in primitive_list:
        key, meta = _component_key(primitive)
        component_items[key].append(primitive)
        component_meta[key] = meta
    closed_regions = build_closed_region_summaries(scene, primitives=primitive_list)
    closed_regions_by_primitive: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for region in closed_regions:
        for primitive_id in region.get("primitive_ids") or []:
            closed_regions_by_primitive[str(primitive_id)].append(region)

    scene_box = _bbox(scene.get("world_bounds")) or [0.0, 0.0, 0.0, 0.0]
    scene_span = max(scene_box[2] - scene_box[0], scene_box[3] - scene_box[1], 1.0)
    max_component_span = max(2500.0, min(10000.0, scene_span * 0.12))
    text_list = list(texts if texts is not None else scene.get("texts") or [])
    native_list = [item for item in native_candidates if isinstance(item, dict)]

    signals_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    signals_by_primitive: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wall_barrier_primitive_ids: set[str] = set()
    for semantic in sympoint_result.get("semantic_by_primitive") or []:
        class_name = _class_name(semantic)
        try:
            class_id = int(semantic.get("class_id"))
        except (TypeError, ValueError):
            class_id = -1
        try:
            score = max(0.0, min(1.0, float(semantic.get("score", 0.0) or 0.0)))
        except (TypeError, ValueError):
            score = 0.0
        subtype = _subtype(class_name)
        primitive_ids = _primitive_ids(semantic, scene)
        # SYP wall masks are more dependable than damaged/non-Unicode layer
        # names.  Use confident wall masks as topology barriers even though a
        # wall is not an open-world equipment signal.
        if "wall" in class_name.casefold() and score >= 0.50:
            wall_barrier_primitive_ids.update(primitive_ids)
        if class_id not in _SYP_THING_IDS and not any(term in class_name.casefold() for term in _EQUIPMENT_TERMS):
            continue
        for primitive_id in primitive_ids:
            primitive = primitive_lookup.get(primitive_id)
            if primitive is None:
                continue
            component_key, _ = _component_key(primitive)
            signal = {
                "primitive_id": primitive_id,
                "class_id": class_id,
                "class_name": class_name,
                "subtype": subtype,
                "score": score,
            }
            signals_by_component[component_key].append(signal)
            signals_by_primitive[primitive_id].append(signal)

    candidates: list[dict[str, Any]] = []
    # An exploded parent block is often much broader than a fixture: it can
    # contain its toilet, a door and wall-adjacent drafting lines.  SYP sees
    # primitives rather than blocks, so use its equipment signal as a seed and
    # expand only through compact, wall-separated existing linework.  This is
    # intentionally before the native-component pass below; otherwise the
    # parent block's provenance would hide the missing fixture fragments.
    isolated_regions = build_isolated_fixture_regions(
        scene,
        primitives=primitive_list,
        seed_primitive_ids=signals_by_primitive.keys(),
        barrier_primitive_ids=wall_barrier_primitive_ids,
    )
    expanded_component_keys: set[str] = set()
    emitted_isolated_regions: set[str] = set()
    for region in isolated_regions:
        region_primitive_ids = {str(item) for item in region.get("primitive_ids") or [] if item}
        seed_ids = {str(item) for item in region.get("seed_primitive_ids") or [] if item}
        signals = [signal for primitive_id in region_primitive_ids for signal in signals_by_primitive.get(primitive_id, []) if signal["score"] >= threshold]
        if not region_primitive_ids or not seed_ids or not signals:
            continue
        seed_component_keys = {
            _component_key(primitive_lookup[primitive_id])[0]
            for primitive_id in seed_ids
            if primitive_id in primitive_lookup
        }
        # Do not replace an already complete native leaf component with the
        # same geometry.  This branch exists only when it recovered additional
        # linework outside the SYP-seeded component.
        source_ids = {
            str(primitive["primitive_id"])
            for component_key in seed_component_keys
            for primitive in component_items.get(component_key, [])
        }
        if region_primitive_ids.issubset(source_ids):
            continue
        region_id = str(region.get("region_id") or "")
        if not region_id or region_id in emitted_isolated_regions:
            continue
        hypotheses = _semantic_hypotheses(signals)
        if not hypotheses:
            continue
        bbox = _bbox(region.get("bbox_world"))
        if bbox is None:
            continue
        top_subtype = str(hypotheses[0]["subtype"])
        max_score = max(float(item.get("max_score", 0.0)) for item in hypotheses)
        nearby_text = _nearby_texts(bbox, text_list)
        direct_text_subtypes = [
            _subtype(item.get("text", ""))
            for item in nearby_text
            if item.get("role") == "equipment_label" and _subtype(item.get("text", "")) != "unknown"
        ]
        if direct_text_subtypes:
            top_subtype = direct_text_subtypes[0]
        existing_labels = _existing_labels(
            region_primitive_ids,
            native_candidates=native_list,
            sympoint_result=sympoint_result,
            scene=scene,
        )
        if top_subtype in existing_labels and not direct_text_subtypes:
            continue
        digest = hashlib.sha1("|".join(sorted(region_primitive_ids)).encode("utf-8", errors="replace")).hexdigest()[:12]
        candidates.append({
            "candidate_id": f"isolated_{scene.get('scene_id', 'scene')}_{digest}",
            "scene_id": scene.get("scene_id"),
            "candidate_kind": "isolated_fixture_gap",
            "type": _type_for_subtype(top_subtype),
            "subtype": top_subtype,
            "subtype_hypotheses": hypotheses,
            "confidence": round(max_score, 4),
            "signal_confidence": round(max_score, 4),
            "confidence_scope": "sympoint_seed_expanded_by_existing_wall_separated_geometry",
            "status": "gap_candidate",
            "geometry_mode": "area" if bbox[2] > bbox[0] and bbox[3] > bbox[1] else "linear",
            "geometry_world": {"bbox": bbox, "polygon": []},
            "source_handles": list(region.get("source_handles") or []),
            "source_entity_ids": list(region.get("source_entity_ids") or []),
            "primitive_ids": sorted(region_primitive_ids),
            "evidence_refs": list(dict.fromkeys([
                *sorted(region_primitive_ids),
                *[str(item.get("text_id")) for item in nearby_text if item.get("text_id")],
            ])),
            "evidence": {
                "isolated_fixture_region": region,
                "geometry_expansion": {
                    "seed_primitive_ids": sorted(seed_ids),
                    "recovered_primitive_ids": sorted(region_primitive_ids - seed_ids),
                    "rule": "existing primitives only; wall lines are boundary evidence and are never absorbed",
                },
                "sympoint_gap": {
                    "min_score_threshold": threshold,
                    "semantic_evidence": signals[:96],
                    "hypotheses": hypotheses,
                    "max_score": round(max_score, 4),
                    "existing_labels_on_overlap": sorted(existing_labels),
                    "scope_note": "SYP primitive semantic score seeds a geometry expansion; it is not object confidence.",
                },
                "nearby_text": nearby_text,
                "direct_text_subtypes": direct_text_subtypes,
                "room_context": _room_context(bbox, text_list),
                "visual_observations": [],
                "conflicts": ([{"code": "SYP_EXISTING_OBJECT_OVERLAP", "existing_labels": sorted(existing_labels)}] if existing_labels else []),
                "uncertainty_codes": ["open_world_candidate", "geometry_fixed_by_scene", "requires_agent_semantic_validation"],
            },
        })
        emitted_isolated_regions.add(region_id)
        expanded_component_keys.update(seed_component_keys)

    seen_component_keys: set[str] = set()
    for component_key, primitives in component_items.items():
        if component_key in expanded_component_keys:
            continue
        geometry = _component_geometry(primitives)
        bbox = geometry.get("bbox_world")
        if bbox is None:
            continue
        if max(geometry.get("width_world", 0.0), geometry.get("height_world", 0.0)) > max_component_span:
            continue
        # A singleton line can be a real symbol fragment, but without a
        # semantic signal or direct label it is not useful to send to an LLM.
        signals = [item for item in signals_by_component.get(component_key, []) if item["score"] >= threshold]
        nearby_text = _nearby_texts(bbox, text_list)
        direct_text_subtypes = [
            _subtype(item.get("text", ""))
            for item in nearby_text
            if item.get("role") == "equipment_label" and _subtype(item.get("text", "")) != "unknown"
        ]
        if not signals and not direct_text_subtypes:
            continue
        if not signals and not nearby_text:
            continue
        hypotheses = _semantic_hypotheses(signals)
        top_subtype = str(hypotheses[0]["subtype"]) if hypotheses else "unknown"
        max_score = max([float(item.get("max_score", 0.0)) for item in hypotheses] or [0.0])
        # A single unrooted line with a near-uniform 1/35 argmax is usually
        # background, not a complete object.  Keep singleton geometry when a
        # direct label or a materially stronger score supports it; composite
        # leaf components are retained even at weak scores for recall.
        if len(primitives) == 1 and max_score < 0.20 and not direct_text_subtypes:
            continue
        if direct_text_subtypes:
            top_subtype = direct_text_subtypes[0]
        existing_labels = _existing_labels(
            {str(item["primitive_id"]) for item in primitives},
            native_candidates=native_list,
            sympoint_result=sympoint_result,
            scene=scene,
        )
        semantic_primitive_ids = {str(item.get("primitive_id")) for item in signals if item.get("primitive_id")}
        recovered_primitive_ids = sorted({str(item["primitive_id"]) for item in primitives} - semantic_primitive_ids)
        # A SYP primitive mask can cover only part of a valid leaf component.
        # Keep the leaf completion candidate even if another source already
        # uses the same subtype: the Agent may need to split/reassign
        # primitive ownership so the final renderer colors every line.
        if top_subtype in existing_labels and not direct_text_subtypes and not recovered_primitive_ids:
            continue
        # Avoid one candidate per semantic row when the source contains a
        # repeated identical primitive group.
        if component_key in seen_component_keys:
            continue
        seen_component_keys.add(component_key)
        primitive_ids = [str(item["primitive_id"]) for item in primitives]
        source_handles = sorted({
            str(value)
            for primitive in primitives
            for value in (
                primitive.get("handle"),
                (primitive.get("source") or {}).get("handle"),
            )
            if value
        })[:32]
        source_entity_ids = sorted({
            str(primitive.get("source_entity_id"))
            for primitive in primitives
            if primitive.get("source_entity_id")
        })[:64]
        room_context = _room_context(bbox, text_list)
        digest = hashlib.sha1(component_key.encode("utf-8", errors="replace")).hexdigest()[:12]
        candidate_id = f"gap_{scene.get('scene_id', 'scene')}_{digest}"
        candidate_type = _type_for_subtype(top_subtype)
        candidate_kind = (
            "text_anchor_gap" if direct_text_subtypes and not signals
            else "sympoint_leaf_completion_candidate" if recovered_primitive_ids
            else "sympoint_gap_candidate"
        )
        candidates.append({
            "candidate_id": candidate_id,
            "scene_id": scene.get("scene_id"),
            "candidate_kind": candidate_kind,
            "type": candidate_type,
            "subtype": top_subtype,
            "subtype_hypotheses": hypotheses,
            "confidence": round(max_score, 4),
            "signal_confidence": round(max_score, 4),
            "confidence_scope": "sympoint_primitive_gap_signal",
            "status": "gap_candidate",
            "geometry_mode": "area" if bbox[2] > bbox[0] and bbox[3] > bbox[1] else "linear",
            "geometry_world": {"bbox": bbox, "polygon": []},
            "source_handles": source_handles,
            "source_entity_ids": source_entity_ids,
            "primitive_ids": primitive_ids,
            "evidence_refs": list(dict.fromkeys([
                *primitive_ids,
                *[str(item.get("text_id")) for item in nearby_text if item.get("text_id")],
            ])),
            "evidence": {
                "native_component": {
                    **component_meta.get(component_key, {}),
                    "component_key": component_key,
                    "geometry_summary": geometry,
                },
                "sympoint_gap": {
                    "min_score_threshold": threshold,
                    "semantic_evidence": signals[:96],
                    "hypotheses": hypotheses,
                    "max_score": round(max_score, 4),
                    "existing_labels_on_overlap": sorted(existing_labels),
                    "scope_note": "SYP primitive semantic score is a gap signal, not object confidence.",
                },
                "geometry_expansion": {
                    "seed_primitive_ids": sorted(semantic_primitive_ids),
                    "recovered_primitive_ids": recovered_primitive_ids,
                    "rule": "complete the existing compact leaf component only; never create or move a CAD primitive",
                },
                "nearby_text": nearby_text,
                "direct_text_subtypes": direct_text_subtypes,
                "room_context": room_context,
                "visual_observations": [],
                "conflicts": ([{"code": "SYP_EXISTING_OBJECT_OVERLAP", "existing_labels": sorted(existing_labels)}] if existing_labels else []),
                "uncertainty_codes": [
                    "open_world_candidate",
                    "geometry_fixed_by_scene",
                    *( ["sympoint_partial_leaf_mask"] if recovered_primitive_ids else [] ),
                ],
                "closed_regions": [
                    region for region in {
                        str(item.get("region_id")): item
                        for primitive in primitives
                        for item in closed_regions_by_primitive.get(str(primitive["primitive_id"]), [])
                    }.values()
                ][:8],
            },
        })
    if include_closed_region_candidates:
        existing_candidate_primitives = {
            str(primitive_id)
            for candidate in candidates
            for primitive_id in candidate.get("primitive_ids") or []
        }
        geometric_candidates: list[dict[str, Any]] = []
        for region in closed_regions:
            region_primitive_ids = {str(item) for item in region.get("primitive_ids") or [] if item}
            if not region_primitive_ids or region_primitive_ids.intersection(existing_candidate_primitives):
                continue
            bbox = _bbox(region.get("bbox_world"))
            if bbox is None or max(region.get("width_world", 0.0), region.get("height_world", 0.0)) > max_component_span:
                continue
            # A closed loop inside a very large exploded group is often a
            # room/container detail rather than one fixture.  Keep those
            # regions in the geometry audit, but do not promote them to an
            # open-world semantic candidate.
            if int(region.get("primitive_count", 0) or 0) > 64:
                continue
            region_primitives = [primitive_lookup[item] for item in region_primitive_ids if item in primitive_lookup]
            if not region_primitives:
                continue
            existing_labels = _existing_labels(
                region_primitive_ids,
                native_candidates=native_list,
                sympoint_result=sympoint_result,
                scene=scene,
            )
            # A closed group already covered by a known door/window/equipment
            # label is not a missing-object candidate.  The Agent should only
            # see genuinely unclassified compact regions here.
            if existing_labels:
                continue
            nearby_text = _nearby_texts(bbox, text_list)
            room_context = _room_context(bbox, text_list)
            digest = hashlib.sha1(
                "|".join(sorted(region_primitive_ids)).encode("utf-8", errors="replace")
            ).hexdigest()[:12]
            geometric_candidates.append({
                "candidate_id": f"closed_{scene.get('scene_id', 'scene')}_{digest}",
                "scene_id": scene.get("scene_id"),
                "candidate_kind": "closed_region_gap",
                "type": "furniture",
                "subtype": "unknown",
                "subtype_hypotheses": [],
                "confidence": 0.0,
                "signal_confidence": 0.0,
                "confidence_scope": "geometry_only_closed_region",
                "status": "gap_candidate",
                "geometry_mode": "area" if bbox[2] > bbox[0] and bbox[3] > bbox[1] else "linear",
                "geometry_world": {"bbox": bbox, "polygon": []},
                "source_handles": list(region.get("source_handles") or []),
                "source_entity_ids": list(region.get("source_entity_ids") or []),
                "primitive_ids": sorted(region_primitive_ids),
                "evidence_refs": sorted(region_primitive_ids),
                "evidence": {
                    "native_component": {"geometry_summary": region},
                    "closed_region": region,
                    "nearby_text": nearby_text,
                    "direct_text_subtypes": [],
                    "room_context": room_context,
                    "visual_observations": [],
                    "conflicts": [],
                    "uncertainty_codes": ["open_world_candidate", "geometry_only_semantics"],
                },
            })
        geometric_candidates.sort(key=lambda item: str(item.get("candidate_id")))
        candidates.extend(geometric_candidates[:max(0, int(max_closed_region_candidates))])
    candidates.sort(key=lambda item: (str(item.get("candidate_kind")), str(item.get("candidate_id"))))
    return candidates
