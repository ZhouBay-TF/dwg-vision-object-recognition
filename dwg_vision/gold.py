from __future__ import annotations

"""Optional AutoCAD gold-label loading and SymPointV2 evaluation.

Gold labels are an offline calibration aid.  They never replace the normal
runtime evidence chain and are not required for production inference.  A
BlockReference is the gold instance; primitives produced by recursive
Explode are only geometry belonging to that root Handle.
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _normalize_handle(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_subtype(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    aliases = {
        "single_door": "single_door",
        "single_door_": "single_door",
        "single_swing_door": "single_door",
        "double_door": "double_door",
        "sliding_door": "sliding_door",
        "unknown": "unknown",
    }
    return aliases.get(text, text)


def _model_subtype(value: dict[str, Any]) -> str:
    text = str(value.get("class_name") or value.get("subtype") or "").strip().lower()
    return _normalize_subtype(text)


def _model_class(value: dict[str, Any]) -> str:
    """Normalize the coarse class used for partial gold-label scoring."""
    subtype = _model_subtype(value)
    if subtype.endswith("_door") or "door" in str(value.get("class_name") or "").lower():
        return "door"
    return _normalize_subtype(str(value.get("class_name") or value.get("subtype") or "unknown"))


def load_gold(path: Path) -> dict[str, Any]:
    """Load and validate a ``cad_gold.v1`` file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "cad_gold.v1":
        raise ValueError(f"unsupported gold schema: {path}")
    catalog = payload.get("symbol_catalog")
    if not isinstance(catalog, list) or not catalog:
        raise ValueError(f"gold symbol_catalog is empty: {path}")
    by_handle: dict[str, dict[str, Any]] = {}
    for entry in catalog:
        if not isinstance(entry, dict):
            raise ValueError("gold symbol_catalog entries must be objects")
        name = str(entry.get("effective_name") or "")
        subtype = _normalize_subtype(entry.get("subtype"))
        handles = entry.get("handles") or []
        if not name or not isinstance(handles, list):
            raise ValueError("gold catalog entry requires effective_name and handles")
        for handle in handles:
            normalized = _normalize_handle(handle)
            if not normalized:
                raise ValueError(f"gold catalog contains an empty Handle: {name}")
            if normalized in by_handle:
                raise ValueError(f"duplicate gold Handle: {normalized}")
            by_handle[normalized] = {
                "handle": normalized,
                "effective_name": name,
                "class": str(entry.get("class") or "unknown"),
                "subtype": subtype or "unknown",
                "confidence": float(entry.get("confidence", 0.0) or 0.0),
            }
    result = dict(payload)
    result["_by_handle"] = by_handle
    return result


def _primitive_root_handle(primitive: dict[str, Any]) -> str:
    source = primitive.get("source") or {}
    provenance = primitive.get("provenance") or {}
    for value in (
        source.get("exploded_from"),
        provenance.get("exploded_from"),
        source.get("block_handle"),
        provenance.get("block_handle"),
    ):
        normalized = _normalize_handle(value)
        if normalized:
            return normalized
    for value in (primitive.get("handle"), primitive.get("source_entity_id")):
        normalized = _normalize_handle(value)
        if ":EXPLODE:" in normalized:
            return normalized.split(":EXPLODE:", 1)[0]
        if normalized:
            return normalized
    return ""


def _result_primitive_ids(instance: dict[str, Any], scene: dict[str, Any]) -> list[str]:
    direct = [str(value) for value in instance.get("primitive_ids") or [] if value]
    if direct:
        return direct
    result: list[str] = []
    primitives = scene.get("primitives") or []
    for index in instance.get("primitive_indices") or []:
        try:
            primitive = primitives[int(index)]
        except (IndexError, TypeError, ValueError):
            continue
        if primitive.get("primitive_id"):
            result.append(str(primitive["primitive_id"]))
    return result


def evaluate_sympoint_against_gold(
    scene: dict[str, Any],
    sympoint_result: dict[str, Any],
    gold: dict[str, Any],
) -> dict[str, Any]:
    """Compare model instances with gold BlockReference roots by geometry.

    A result is ``correct`` only when a model door instance overlaps the
    expected root's exploded primitives and has the verified subtype.  Gold
    entries marked ``unknown`` are scored for class/coverage only and are not
    penalized for an unresolved subtype.
    """
    by_handle = dict(gold.get("_by_handle") or {})
    gold_classes = {str(item.get("class") or "unknown").strip().lower() for item in by_handle.values()}
    primitive_by_id = {
        str(item.get("primitive_id")): item
        for item in scene.get("primitives") or []
        if item.get("primitive_id")
    }
    root_to_primitive_ids: dict[str, set[str]] = defaultdict(set)
    root_to_geometry_ids: dict[str, set[str]] = defaultdict(set)
    for primitive_id, primitive in primitive_by_id.items():
        root = _primitive_root_handle(primitive)
        if root:
            root_to_primitive_ids[root].add(primitive_id)
            if not primitive.get("is_block_proxy") and str(primitive.get("command") or "") != "block":
                root_to_geometry_ids[root].add(primitive_id)

    model_instances: list[dict[str, Any]] = []
    for index, instance in enumerate(sympoint_result.get("instances") or []):
        primitive_ids = set(_result_primitive_ids(instance, scene)) & set(primitive_by_id)
        if not primitive_ids:
            continue
        model_instances.append({
            "index": index,
            "instance_id": str(instance.get("instance_id") or f"spv_{index:04d}"),
            "class_name": str(instance.get("class_name") or "unknown"),
            "subtype": _model_subtype(instance),
            "coarse_class": _model_class(instance),
            "class_id": instance.get("class_id"),
            "score": float(instance.get("score", 0.0) or 0.0),
            "primitive_ids": primitive_ids,
        })

    gold_rows: list[dict[str, Any]] = []
    covered_model_indices: set[int] = set()
    for handle, expected in sorted(by_handle.items()):
        expected_ids = root_to_primitive_ids.get(handle, set())
        geometry_ids = root_to_geometry_ids.get(handle, set())
        candidates: list[dict[str, Any]] = []
        for model in model_instances:
            overlap = expected_ids & model["primitive_ids"]
            if not overlap:
                continue
            covered_model_indices.add(model["index"])
            candidates.append({
                "instance_id": model["instance_id"],
                "class_name": model["class_name"],
                "subtype": model["subtype"],
                "score": round(model["score"], 4),
                "overlap_primitive_count": len(overlap),
                "overlap_ratio_of_gold_geometry": round(len(overlap) / max(1, len(expected_ids)), 4),
            })

        expected_subtype = expected["subtype"]
        door_candidates = [item for item in candidates if "door" in item["class_name"].lower() or item["subtype"].endswith("_door")]
        matching = [item for item in door_candidates if item["subtype"] == expected_subtype]
        if not expected_ids:
            # The parent Scene used for fusion is intentionally stripped of
            # unexpanded BlockReference footprints.  A gold Handle absent
            # from that model input is therefore an input-geometry gap, not
            # a semantic miss by SymPointV2.
            status = "input_geometry_unavailable"
        elif not geometry_ids:
            status = "input_geometry_unavailable"
        elif not candidates:
            status = "missed"
        elif expected_subtype == "unknown":
            status = "class_covered_subtype_unverified" if door_candidates else "wrong_class"
        elif matching:
            status = "correct"
        elif door_candidates:
            status = "wrong_subtype"
        else:
            status = "wrong_class"
        gold_rows.append({
            "gold_handle": handle,
            "effective_name": expected["effective_name"],
            "expected_class": expected["class"],
            "expected_subtype": expected_subtype,
            "gold_primitive_count": len(expected_ids),
            "input_geometry_available": bool(geometry_ids),
            "input_geometry_primitive_count": len(geometry_ids),
            "status": status,
            "model_candidates": candidates,
        })

    verified_rows = [row for row in gold_rows if row["expected_subtype"] != "unknown"]
    scorable_verified_rows = [
        row for row in verified_rows if row["status"] != "input_geometry_unavailable"
    ]
    # A catalog can intentionally be partial (the supplied catalog labels
    # doors only).  Do not call every unlisted furniture/window/wall result a
    # false positive; report those as unscored model output instead.
    in_scope_model_indices = {
        model["index"] for model in model_instances if model["coarse_class"] in gold_classes
    }
    predicted_false_positives = [
        {
            "instance_id": model["instance_id"],
            "class_name": model["class_name"],
            "subtype": model["subtype"],
            "score": round(model["score"], 4),
            "primitive_ids": sorted(model["primitive_ids"]),
        }
        for model in model_instances
        if model["index"] in in_scope_model_indices and model["index"] not in covered_model_indices
    ]
    unscored_model_instances = [
        {
            "instance_id": model["instance_id"],
            "class_name": model["class_name"],
            "subtype": model["subtype"],
            "score": round(model["score"], 4),
        }
        for model in model_instances
        if model["index"] not in in_scope_model_indices
    ]
    status_counts = Counter(row["status"] for row in gold_rows)
    expected_counts = Counter(row["expected_subtype"] for row in gold_rows)
    return {
        "schema_version": "cad_gold_evaluation.v1",
        "scene_id": str(scene.get("scene_id") or ""),
        "gold_source": gold.get("source") or {},
        "gold_label_scope": sorted(gold_classes),
        "gold_instance_count": len(gold_rows),
        "gold_instances_present_in_scene": sum(
            row["status"] not in {"gold_handle_not_in_scene", "input_geometry_unavailable"}
            for row in gold_rows
        ),
        "verified_gold_count": len(verified_rows),
        "scorable_verified_gold_count": len(scorable_verified_rows),
        "input_geometry_unavailable_count": sum(
            row["status"] == "input_geometry_unavailable" for row in gold_rows
        ),
        "expected_subtype_counts": dict(sorted(expected_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "model_instance_count": len(model_instances),
        "model_subtype_counts": dict(sorted(Counter(model["subtype"] for model in model_instances).items())),
        "verified_metrics": {
            "correct": status_counts.get("correct", 0),
            "wrong_subtype": status_counts.get("wrong_subtype", 0),
            "wrong_class": status_counts.get("wrong_class", 0),
            "missed": status_counts.get("missed", 0),
            "input_geometry_unavailable": status_counts.get("input_geometry_unavailable", 0),
            "scorable_count": len(scorable_verified_rows),
            "coverage": round(
                sum(row["status"] == "correct" for row in scorable_verified_rows)
                / max(1, len(scorable_verified_rows)),
                4,
            ),
        },
        "predicted_false_positive_count": len(predicted_false_positives),
        "predicted_false_positives": predicted_false_positives,
        "unscored_model_instance_count": len(unscored_model_instances),
        "unscored_model_instances": unscored_model_instances,
        "instances": gold_rows,
        "policy": "gold is offline evaluation evidence; it does not mutate production Scene Graph objects",
    }


def strip_internal_fields(gold: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable copy without the private handle index."""
    return {key: value for key, value in gold.items() if not str(key).startswith("_")}
