from __future__ import annotations

"""Evidence-bounded CAD reasoning over native AutoCAD objects.

The reasoning model never receives authority to create geometry.  AutoCAD
BlockReference handles and exploded child geometry are first converted into a
deterministic catalog; an optional model may only name catalog families and
must cite catalog evidence IDs.
"""

import json
import math
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Protocol

from .contracts import require_bbox
from .env_config import env_first
from .openai_config import load_openai_compatible_config


CAD_OBJECT_TYPES = {"wall", "window", "door", "furniture", "unknown"}
_GEOMETRY_MUTATION_KEYS = {
    "bbox", "bbox_world", "polygon", "polygon_world", "geometry", "handles",
    "source_handles", "primitive_ids", "coordinates", "points",
}


class CADReasoningProvider(Protocol):
    name: str
    model: str

    def reason(self, catalog: dict[str, Any]) -> dict[str, Any]:
        ...


def _safe_bbox(value: Any) -> list[float] | None:
    try:
        box = require_bbox(value, "native bbox")
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in box):
        return None
    return box


def _bbox_union(boxes: Iterable[list[float]]) -> list[float] | None:
    values = [box for item in boxes if (box := _safe_bbox(item)) is not None]
    if not values:
        return None
    return [
        min(item[0] for item in values),
        min(item[1] for item in values),
        max(item[2] for item in values),
        max(item[3] for item in values),
    ]


def _root_handle(entity: dict[str, Any]) -> str:
    provenance = entity.get("provenance") or {}
    return str(
        provenance.get("exploded_from")
        or provenance.get("native_root_handle")
        or entity.get("exploded_from")
        or ""
    )


def _counter_dict(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(str(value or "unknown") for value in values).items()))


def _round_number(value: Any, digits: int = 4) -> float:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


def _reference_geometry(children: list[dict[str, Any]]) -> dict[str, Any]:
    boxes = [item.get("bbox_world") for item in children if item.get("bbox_world")]
    bbox = _bbox_union(boxes)
    command_counts = _counter_dict(str(item.get("command") or "unknown") for item in children)
    entity_type_counts = _counter_dict(str(item.get("entity_type") or "unknown") for item in children)
    closed_polylines = sum(
        1 for item in children
        if str(item.get("command") or "").lower() == "polyline"
        and bool((item.get("dimensions") or {}).get("closed"))
    )
    return {
        "child_count": len(children),
        "command_counts": command_counts,
        "entity_type_counts": entity_type_counts,
        "closed_polyline_count": closed_polylines,
        "bbox_world": bbox,
        "width_world": _round_number(bbox[2] - bbox[0]) if bbox else 0.0,
        "height_world": _round_number(bbox[3] - bbox[1]) if bbox else 0.0,
    }


def _line_orientation_bucket(entity: dict[str, Any]) -> str:
    points = entity.get("points_world") or entity.get("polygon_world") or []
    if len(points) < 2:
        return "unknown"
    try:
        dx = float(points[-1][0]) - float(points[0][0])
        dy = float(points[-1][1]) - float(points[0][1])
    except (IndexError, TypeError, ValueError):
        return "unknown"
    if math.hypot(dx, dy) <= 1e-9:
        return "point"
    angle = math.degrees(math.atan2(dy, dx)) % 180.0
    if angle <= 7.5 or angle >= 172.5:
        return "horizontal"
    if 82.5 <= angle <= 97.5:
        return "vertical"
    if 37.5 <= angle <= 52.5:
        return "diagonal_45"
    if 127.5 <= angle <= 142.5:
        return "diagonal_135"
    return "other"


def _linework_group_geometry(entities: list[dict[str, Any]]) -> dict[str, Any]:
    boxes = [item.get("bbox_world") for item in entities if item.get("bbox_world")]
    bbox = _bbox_union(boxes)
    lengths: list[float] = []
    for entity in entities:
        dimensions = entity.get("dimensions") or {}
        try:
            length = float(dimensions.get("length") or 0.0)
        except (TypeError, ValueError):
            length = 0.0
        if length <= 0.0:
            item_bbox = _safe_bbox(entity.get("bbox_world"))
            if item_bbox:
                length = math.hypot(item_bbox[2] - item_bbox[0], item_bbox[3] - item_bbox[1])
        if length > 0.0:
            lengths.append(length)
    lengths.sort()
    median = lengths[len(lengths) // 2] if lengths else 0.0
    return {
        # Reuse child_count as an evidence-completeness signal in the shared
        # confidence cap.  Here it means source entity count, not block child
        # count; family_kind disambiguates the semantics.
        "child_count": len(entities),
        "source_entity_count": len(entities),
        "command_counts": _counter_dict(str(item.get("command") or "unknown") for item in entities),
        "entity_type_counts": _counter_dict(str(item.get("entity_type") or "unknown") for item in entities),
        "orientation_counts": _counter_dict(_line_orientation_bucket(item) for item in entities),
        "closed_polyline_count": sum(
            1 for item in entities if bool((item.get("dimensions") or {}).get("closed"))
        ),
        "bbox_world": bbox,
        "width_world": _round_number(bbox[2] - bbox[0]) if bbox else 0.0,
        "height_world": _round_number(bbox[3] - bbox[1]) if bbox else 0.0,
        "length_min_world": _round_number(lengths[0]) if lengths else 0.0,
        "length_median_world": _round_number(median),
        "length_max_world": _round_number(lengths[-1]) if lengths else 0.0,
    }


def _nested_component_key(entity: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return a stable leaf-block grouping key for exploded native geometry."""
    provenance = entity.get("provenance") or {}
    root_handle = str(provenance.get("handle") or entity.get("native_root_handle") or "")
    block_name = str(provenance.get("block_name") or "").strip()
    parent_path = str(provenance.get("exploded_from") or entity.get("exploded_from") or "").strip()
    if not root_handle or not block_name or not parent_path:
        return None
    return root_handle, block_name, parent_path, f"{root_handle}|{block_name}|{parent_path}"


def _nested_component_record(
    root_handle: str,
    block_name: str,
    parent_path: str,
    entities: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    geometry = _reference_geometry(entities)
    source_entity_ids = [
        str(item.get("entity_id") or item.get("handle") or "")
        for item in entities
        if item.get("entity_id") or item.get("handle")
    ]
    return {
        "component_id": f"component:{root_handle}:{index:04d}",
        "native_root_handle": root_handle,
        "block_name": block_name,
        "parent_path": parent_path,
        "source_entity_ids": source_entity_ids[:128],
        "primitive_count": len(entities),
        "geometry_summary": geometry,
        "layer_counts": _counter_dict(
            str(item.get("layer") or (item.get("provenance") or {}).get("layer") or "")
            for item in entities
        ),
        "evidence_refs": [
            f"native_component:{root_handle}:{index:04d}",
            *[f"native_geometry:{item}" for item in source_entity_ids[:8]],
        ],
    }


def _nested_reference_component(
    node: dict[str, Any],
    entities: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    """Convert an exported nested BlockReference into a semantic component.

    Unlike the legacy ``nested_components`` assembled from Explode provenance,
    this record is anchored to the actual nested reference path and its
    definition name.  It is therefore safe for the CAD Agent to name a child
    such as ``马桶(730)`` without promoting the whole bathroom parent block.
    """
    root_handle = str(node.get("root_handle") or "")
    reference_id = str(node.get("reference_id") or node.get("handle") or f"node_{index:04d}")
    source_entity_ids = [
        str(item.get("entity_id") or item.get("handle") or "")
        for item in entities
        if item.get("entity_id") or item.get("handle")
    ]
    geometry = _reference_geometry(entities)
    if not geometry.get("bbox_world"):
        geometry["bbox_world"] = _safe_bbox(node.get("bbox_world"))
    component_id = f"component:{root_handle}:blockref:{reference_id}"
    definition_name = str(
        node.get("effective_name") or node.get("definition_name") or node.get("name") or ""
    )
    return {
        "component_id": component_id,
        "component_kind": "nested_block_reference",
        "reference_id": reference_id,
        "native_root_handle": root_handle,
        "block_name": definition_name,
        "definition_name": str(node.get("definition_name") or definition_name),
        "effective_name": str(node.get("effective_name") or definition_name),
        "parent_path": str(node.get("parent_reference_id") or ""),
        "reference_path": list(node.get("reference_path") or []),
        "source_entity_ids": source_entity_ids[:256],
        "primitive_ids": source_entity_ids[:256],
        "primitive_count": len(entities),
        "geometry_summary": geometry,
        "layer_counts": _counter_dict(
            str(item.get("layer") or (item.get("provenance") or {}).get("layer") or "")
            for item in entities
        ) or _counter_dict([str(node.get("layer") or "")]),
        "rotation_deg": _round_number(node.get("rotation_deg"), 4),
        "scale": dict(node.get("scale") or {}),
        "bbox_world": _safe_bbox(node.get("bbox_world")) or geometry.get("bbox_world"),
        "evidence_refs": [
            f"native_nested_block:{reference_id}",
            f"native_definition:{definition_name}",
            *[f"native_geometry:{item}" for item in source_entity_ids[:8]],
        ],
    }


def build_native_evidence_catalog(raw: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, auditable equivalent of AutoCAD query tool calls."""

    blocks = [dict(item) for item in raw.get("native_blocks") or [] if isinstance(item, dict)]
    block_reference_tree = [
        dict(item) for item in raw.get("block_reference_tree") or [] if isinstance(item, dict)
    ]
    raw_by_id = {
        str(item.get("entity_id") or item.get("handle")): item
        for item in raw.get("raw_entities") or []
        if isinstance(item, dict) and (item.get("entity_id") or item.get("handle"))
    }
    nested_nodes_by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in block_reference_tree:
        if node.get("parent_reference_id"):
            nested_nodes_by_root[str(node.get("root_handle") or "")].append(node)
    children_by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity in raw.get("raw_entities") or []:
        if not isinstance(entity, dict):
            continue
        root = _root_handle(entity)
        if root:
            children_by_root[root].append(entity)

    nested_groups_by_root: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    nested_meta_by_key: dict[tuple[str, str], tuple[str, str]] = {}
    for entity in raw.get("raw_entities") or []:
        if not isinstance(entity, dict):
            continue
        key = _nested_component_key(entity)
        if key is None:
            continue
        root_handle, block_name, parent_path, group_key = key
        nested_groups_by_root[root_handle][group_key].append(entity)
        nested_meta_by_key[(root_handle, group_key)] = (block_name, parent_path)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        effective_name = str(block.get("effective_name") or block.get("name") or "").strip()
        key = effective_name.casefold() or f"__handle__:{block.get('handle') or block.get('entity_id')}"
        grouped[key].append(block)

    families: list[dict[str, Any]] = []
    for family_index, key in enumerate(sorted(grouped)):
        references = grouped[key]
        family_id = f"family_{family_index:04d}"
        reference_records: list[dict[str, Any]] = []
        for block in sorted(references, key=lambda item: str(item.get("handle") or item.get("entity_id") or "")):
            handle = str(block.get("handle") or block.get("entity_id") or "")
            geometry = _reference_geometry(children_by_root.get(handle, []))
            bbox = geometry.get("bbox_world") or _safe_bbox(block.get("bbox_world"))
            scale = block.get("scale") or {}
            scale_x = _round_number(scale.get("x", 1.0), 6)
            scale_y = _round_number(scale.get("y", 1.0), 6)
            nested_components: list[dict[str, Any]] = []
            component_groups = nested_groups_by_root.get(handle) or {}
            for component_index, group_key in enumerate(sorted(component_groups)):
                component_entities = component_groups[group_key]
                component_name, parent_path = nested_meta_by_key[(handle, group_key)]
                nested_components.append(_nested_component_record(
                    handle,
                    component_name,
                    parent_path,
                    component_entities,
                    component_index,
                ))
            nested_block_references: list[dict[str, Any]] = []
            existing_component_reference_ids = {
                str(item.get("reference_id") or "")
                for item in nested_components
                if item.get("reference_id")
            }
            for node_index, node in enumerate(sorted(
                nested_nodes_by_root.get(handle, []),
                key=lambda item: (
                    int(item.get("depth", 0) or 0),
                    str(item.get("reference_id") or item.get("handle") or ""),
                ),
            )):
                node_ids = [
                    str(item)
                    for item in node.get("primitive_ids") or []
                    if str(item) in raw_by_id
                ]
                node_path = [str(item) for item in node.get("reference_path") or [] if item]
                if not node_ids and node_path:
                    node_ids = [
                        entity_id for entity_id, entity in raw_by_id.items()
                        if node_path == [str(item) for item in (entity.get("provenance") or {}).get("block_reference_path") or []][:len(node_path)]
                    ]
                component_entities = [raw_by_id[item] for item in node_ids if item in raw_by_id]
                component = _nested_reference_component(node, component_entities, node_index)
                nested_block_references.append({
                    "reference_id": node.get("reference_id") or node.get("handle"),
                    "handle": node.get("handle") or node.get("reference_id"),
                    "parent_reference_id": node.get("parent_reference_id"),
                    "reference_path": list(node.get("reference_path") or []),
                    "depth": int(node.get("depth", 0) or 0),
                    "definition_name": node.get("definition_name") or "",
                    "effective_name": node.get("effective_name") or node.get("definition_name") or "",
                    "layer": node.get("layer") or "",
                    "bbox_world": component.get("bbox_world") or node.get("bbox_world"),
                    "rotation_deg": node.get("rotation_deg", 0.0),
                    "scale": node.get("scale", {}),
                    "primitive_ids": node_ids[:256],
                    "primitive_count": len(node_ids),
                    "geometry_summary": component.get("geometry_summary", {}),
                    "evidence_refs": component.get("evidence_refs", []),
                })
                reference_id = str(node.get("reference_id") or node.get("handle") or "")
                definition_name = str(node.get("effective_name") or node.get("definition_name") or "")
                # Add every named nested reference with geometry as an exact
                # component. Anonymous intermediate containers remain in the
                # audit tree but are not forced into semantic decisions.
                if (
                    reference_id
                    and reference_id not in existing_component_reference_ids
                    and node_ids
                    and definition_name
                    and not definition_name.startswith(("*", "A$C", "$"))
                ):
                    nested_components.append(_nested_reference_component(node, component_entities, node_index))
                    existing_component_reference_ids.add(reference_id)
            reference_records.append({
                "handle": handle,
                "entity_id": str(block.get("entity_id") or handle),
                "name": str(block.get("name") or ""),
                "effective_name": str(block.get("effective_name") or block.get("name") or ""),
                "native_type": str(block.get("type") or "unknown").lower(),
                "layer": str(block.get("layer") or ""),
                "space": str(block.get("space") or ""),
                "bbox_world": bbox,
                "rotation_deg": _round_number(block.get("rotation_deg"), 4),
                "scale": {"x": scale_x, "y": scale_y, "z": _round_number(scale.get("z", 1.0), 6)},
                # A 2-D reflection has a negative determinant.  Two negative
                # axes form a 180-degree orientation change, not a mirror.
                "is_reflected_2d": scale_x * scale_y < 0.0,
                "is_dynamic": bool(block.get("is_dynamic")),
                "expanded": bool(block.get("expanded")),
                "geometry_summary": geometry,
                "nested_components": nested_components,
                "nested_block_references": nested_block_references,
                "evidence_refs": [
                    f"native_block:{handle}",
                    f"native_geometry:{handle}",
                ],
            })

        representative = max(
            reference_records,
            key=lambda item: int((item.get("geometry_summary") or {}).get("child_count") or 0),
            default={},
        )
        effective_names = sorted({item["effective_name"] for item in reference_records if item["effective_name"]})
        family_evidence_refs = [f"native_family:{family_id}"]
        for item in reference_records[:3]:
            family_evidence_refs.extend(item["evidence_refs"])
        families.append({
            "family_id": family_id,
            "family_kind": "block_reference",
            "effective_name": effective_names[0] if effective_names else "",
            "block_names": sorted({item["name"] for item in reference_records if item["name"]}),
            "reference_count": len(reference_records),
            "native_type_counts": _counter_dict(item["native_type"] for item in reference_records),
            "layer_counts": _counter_dict(item["layer"] for item in reference_records),
            "space_counts": _counter_dict(item["space"] for item in reference_records),
            "rotation_values_deg": sorted({_round_number(item["rotation_deg"], 2) for item in reference_records})[:24],
            "reflected_2d_count": sum(1 for item in reference_records if item["is_reflected_2d"]),
            "dynamic_count": sum(1 for item in reference_records if item["is_dynamic"]),
            "expanded_count": sum(1 for item in reference_records if item["expanded"]),
            "representative_geometry": representative.get("geometry_summary") or {},
            "representative_handle": representative.get("handle") or "",
            "references": reference_records,
            "evidence_refs": list(dict.fromkeys(family_evidence_refs)),
        })

    # Standalone linework is not represented by BlockReference families.  It
    # is still valid native CAD evidence and must be visible to the reasoning
    # branch; otherwise layers such as 栏杆/看线/线脚 can fall through both
    # block reasoning and the deterministic offline SymPoint fallback.
    linework_by_layer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity in raw.get("raw_entities") or []:
        if not isinstance(entity, dict) or _root_handle(entity):
            continue
        if entity.get("is_frame"):
            continue
        if str(entity.get("type") or "unknown").lower() != "unknown":
            continue
        command = str(entity.get("command") or "").lower()
        if command not in {"line", "polyline", "arc", "circle", "ellipse"}:
            continue
        layer = str(entity.get("layer") or (entity.get("provenance") or {}).get("layer") or "").strip()
        if not layer or layer == "0":
            continue
        linework_by_layer[layer.casefold()].append(entity)

    for group_index, layer_key in enumerate(sorted(linework_by_layer)):
        entities = linework_by_layer[layer_key]
        layer_name = str(entities[0].get("layer") or (entities[0].get("provenance") or {}).get("layer") or "")
        family_id = f"linework_{group_index:04d}"
        references: list[dict[str, Any]] = []
        for entity in sorted(entities, key=lambda item: str(item.get("handle") or item.get("entity_id") or "")):
            handle = str(entity.get("handle") or entity.get("entity_id") or "")
            bbox = _safe_bbox(entity.get("bbox_world"))
            references.append({
                "handle": handle,
                "entity_id": str(entity.get("entity_id") or handle),
                "name": "",
                "effective_name": f"layer:{layer_name}",
                "native_type": "unknown",
                "layer": layer_name,
                "space": str((entity.get("provenance") or {}).get("space") or ""),
                "bbox_world": bbox,
                "rotation_deg": _round_number(entity.get("rotation_deg"), 4),
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "is_reflected_2d": False,
                "is_dynamic": False,
                "expanded": False,
                "geometry_summary": {
                    "child_count": 1,
                    "command_counts": {str(entity.get("command") or "unknown"): 1},
                    "entity_type_counts": {str(entity.get("entity_type") or "unknown"): 1},
                    "orientation": _line_orientation_bucket(entity),
                    "bbox_world": bbox,
                },
                "evidence_refs": [
                    f"native_linework:{handle}",
                    f"native_geometry:{handle}",
                ],
            })
        geometry = _linework_group_geometry(entities)
        evidence_refs = [f"native_family:{family_id}"]
        for item in references[:3]:
            evidence_refs.extend(item["evidence_refs"])
        families.append({
            "family_id": family_id,
            "family_kind": "linework_layer",
            "effective_name": f"layer:{layer_name}",
            "block_names": [],
            "reference_count": len(references),
            "native_type_counts": {"unknown": len(references)},
            "layer_counts": {layer_name: len(references)},
            "space_counts": _counter_dict(item["space"] for item in references),
            "rotation_values_deg": [],
            "reflected_2d_count": 0,
            "dynamic_count": 0,
            "expanded_count": 0,
            "representative_geometry": geometry,
            "representative_handle": references[0]["handle"] if references else "",
            "references": references,
            "evidence_refs": list(dict.fromkeys(evidence_refs)),
        })

    object_type_counts = _counter_dict(str(item.get("type") or "unknown") for item in blocks)
    trace = {
        "schema_version": "cad_reasoning_trace.v1",
        "actions": [
            {
                "action": "discover_autocad_types",
                "result": {"block_reference_count": len(blocks), "native_type_counts": object_type_counts},
            },
            {
                "action": "aggregate_autocad_objects",
                "result": {
                    "family_count": len(families),
                    "families": [
                        {
                            "family_id": item["family_id"],
                            "effective_name": item["effective_name"],
                            "reference_count": item["reference_count"],
                            "layer_counts": item["layer_counts"],
                        }
                        for item in families
                    ],
                },
            },
            {
                "action": "query_autocad_objects",
                "result": {
                    "fields": [
                        "handle", "effective_name", "layer", "space", "bbox_world",
                        "rotation_deg", "scale", "is_reflected_2d", "geometry_summary",
                    ],
                    "geometry_source": "AutoCAD BlockReference plus non-mutating recursive Explode() evidence",
                },
            },
        ],
    }
    return {
        "schema_version": "cad_evidence_catalog.v1",
        "source_schema": str(raw.get("schema_version") or ""),
        "families": families,
        "summary": {
            "block_reference_count": len(blocks),
            "family_count": len(families),
            "block_family_count": len(grouped),
            "linework_group_count": len(linework_by_layer),
            "exploded_child_count": sum(len(items) for items in children_by_root.values()),
            "native_type_counts": object_type_counts,
            "block_definition_count": len(raw.get("block_definitions") or []),
            "block_reference_tree_count": len(block_reference_tree),
            "nested_block_reference_count": len([item for item in block_reference_tree if item.get("parent_reference_id")]),
        },
        "block_definitions": [dict(item) for item in raw.get("block_definitions") or [] if isinstance(item, dict)],
        "block_reference_tree": block_reference_tree,
        "trace": trace,
    }


def _model_family(family: dict[str, Any]) -> dict[str, Any]:
    representative = dict(family.get("representative_geometry") or {})
    representative.pop("bbox_world", None)
    references = []
    family_native_types = {
        str(value)
        for value, count in (family.get("native_type_counts") or {}).items()
        if str(value) != "unknown" and int(count or 0) > 0
    }
    include_components = not family_native_types
    for reference in family.get("references") or []:
        compact = _compact_reference(reference)
        compact["nested_components"] = [
            {
                "component_id": item.get("component_id"),
                "component_kind": item.get("component_kind", "legacy_exploded_group"),
                "reference_id": item.get("reference_id"),
                "native_root_handle": item.get("native_root_handle"),
                "block_name": item.get("block_name"),
                "definition_name": item.get("definition_name"),
                "effective_name": item.get("effective_name"),
                "parent_path": item.get("parent_path"),
                "reference_path": list(item.get("reference_path") or []),
                "primitive_count": item.get("primitive_count"),
                "geometry_summary": dict(item.get("geometry_summary") or {}),
                "layer_counts": item.get("layer_counts", {}),
                "evidence_refs": list(item.get("evidence_refs") or [])[:6],
            }
            for item in (reference.get("nested_components") or [])[:48]
        ] if include_components else []
        references.append(compact)
    if len(references) > 16:
        stride = max(1, len(references) // 16)
        references = [item for index, item in enumerate(references) if index % stride == 0][:16]
    return {
        "family_id": family["family_id"],
        "family_kind": family.get("family_kind", "block_reference"),
        "effective_name": family.get("effective_name", ""),
        "block_names": family.get("block_names", []),
        "reference_count": family.get("reference_count", 0),
        "native_type_counts": family.get("native_type_counts", {}),
        "layer_counts": family.get("layer_counts", {}),
        "rotation_values_deg": family.get("rotation_values_deg", []),
        "reflected_2d_count": family.get("reflected_2d_count", 0),
        "dynamic_count": family.get("dynamic_count", 0),
        "expanded_count": family.get("expanded_count", 0),
        "representative_geometry": representative,
        "nested_block_reference_count": sum(
            len(item.get("nested_block_references") or [])
            for item in (family.get("references") or [])
            if isinstance(item, dict)
        ),
        "references": references,
        "evidence_refs": family.get("evidence_refs", []),
    }


_ROOM_CONTEXT_RULES: dict[str, dict[str, Any]] = {
    "卫生间": {
        "room_type": "bathroom",
        "likely_subtypes": ["toilet", "squat_toilet", "urinal", "sink", "bath_tub", "shower", "washing_machine"],
        "unlikely_subtypes": ["bed", "sofa", "wardrobe", "dining_table", "tv_cabinet"],
    },
    "厨房": {
        "room_type": "kitchen",
        "likely_subtypes": ["sink", "gas_stove", "refrigerator", "cabinet"],
        "unlikely_subtypes": ["bed", "toilet", "sofa"],
    },
    "主卧室": {
        "room_type": "bedroom",
        "likely_subtypes": ["bed", "wardrobe", "cabinet"],
        "unlikely_subtypes": ["toilet", "gas_stove", "sink"],
    },
    "卧室": {
        "room_type": "bedroom",
        "likely_subtypes": ["bed", "wardrobe", "cabinet"],
        "unlikely_subtypes": ["toilet", "gas_stove", "sink"],
    },
    "客厅": {
        "room_type": "living_room",
        "likely_subtypes": ["sofa", "table", "chair", "tv_cabinet"],
        "unlikely_subtypes": ["toilet", "gas_stove", "bed"],
    },
    "餐厅": {
        "room_type": "dining_room",
        "likely_subtypes": ["table", "chair", "cabinet"],
        "unlikely_subtypes": ["toilet", "bed", "gas_stove"],
    },
    "阳台": {
        "room_type": "balcony",
        "likely_subtypes": ["washing_machine", "cabinet", "railing"],
        "unlikely_subtypes": ["bed", "toilet", "gas_stove"],
    },
}


def _bbox_center(value: Any) -> tuple[float, float] | None:
    box = _safe_bbox(value)
    if box is None:
        return None
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _bbox_intersects(first: Any, second: Any) -> bool:
    a = _safe_bbox(first)
    b = _safe_bbox(second)
    if a is None or b is None:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _compact_reference(reference: dict[str, Any]) -> dict[str, Any]:
    geometry = dict(reference.get("geometry_summary") or {})
    geometry.pop("bbox_world", None)
    return {
        "handle": reference.get("handle", ""),
        "bbox_world": reference.get("bbox_world"),
        "layer": reference.get("layer", ""),
        "space": reference.get("space", ""),
        "native_type": reference.get("native_type", "unknown"),
        "rotation_deg": reference.get("rotation_deg", 0.0),
        "scale": reference.get("scale", {}),
        "is_reflected_2d": bool(reference.get("is_reflected_2d")),
        "geometry_summary": geometry,
        "nested_components": [
            {
                "component_id": item.get("component_id"),
                "component_kind": item.get("component_kind", "legacy_exploded_group"),
                "reference_id": item.get("reference_id"),
                "native_root_handle": item.get("native_root_handle"),
                "block_name": item.get("block_name"),
                "definition_name": item.get("definition_name"),
                "effective_name": item.get("effective_name"),
                "parent_path": item.get("parent_path"),
                "reference_path": list(item.get("reference_path") or []),
                "primitive_count": item.get("primitive_count"),
                "geometry_summary": dict(item.get("geometry_summary") or {}),
                "layer_counts": item.get("layer_counts", {}),
                "evidence_refs": list(item.get("evidence_refs") or [])[:6],
            }
            for item in (reference.get("nested_components") or [])[:48]
        ],
        "nested_block_references": [
            {
                "reference_id": item.get("reference_id"),
                "handle": item.get("handle"),
                "parent_reference_id": item.get("parent_reference_id"),
                "reference_path": list(item.get("reference_path") or []),
                "depth": item.get("depth", 0),
                "definition_name": item.get("definition_name", ""),
                "effective_name": item.get("effective_name", ""),
                "layer": item.get("layer", ""),
                "bbox_world": item.get("bbox_world"),
                "primitive_count": item.get("primitive_count", 0),
                "primitive_ids": list(item.get("primitive_ids") or [])[:64],
                "geometry_summary": dict(item.get("geometry_summary") or {}),
                "evidence_refs": list(item.get("evidence_refs") or [])[:8],
            }
            for item in (reference.get("nested_block_references") or [])[:64]
        ],
    }


def _reference_sample(family: dict[str, Any], scene_bounds: list[float], limit: int = 32) -> list[dict[str, Any]]:
    references = [
        item for item in family.get("references") or []
        if isinstance(item, dict) and _bbox_intersects(item.get("bbox_world"), scene_bounds)
    ]
    references.sort(key=lambda item: str(item.get("handle") or item.get("entity_id") or ""))
    native_types = {
        str(value)
        for value, count in (family.get("native_type_counts") or {}).items()
        if str(value) != "unknown" and int(count or 0) > 0
    }
    include_components = not native_types

    def compact(reference: dict[str, Any]) -> dict[str, Any]:
        value = _compact_reference(reference)
        if not include_components:
            value["nested_components"] = []
        return value

    if len(references) <= limit:
        return [compact(item) for item in references]
    # Preserve spatial coverage instead of only sending the first handles of a
    # large standalone layer.  The complete native catalog remains on disk;
    # this is only a bounded prompt representation.
    selected: list[dict[str, Any]] = []
    stride = max(1, len(references) // limit)
    for index in range(0, len(references), stride):
        selected.append(references[index])
        if len(selected) >= limit:
            break
    return [compact(item) for item in selected]


def _symmetry_summary(references: list[dict[str, Any]], scene_bounds: list[float]) -> dict[str, Any]:
    centers = [center for item in references if (center := _bbox_center(item.get("bbox_world"))) is not None]
    if len(centers) < 2:
        return {"reference_count": len(centers), "x_mirror_matches": 0, "y_mirror_matches": 0}
    center_x = (scene_bounds[0] + scene_bounds[2]) / 2.0
    center_y = (scene_bounds[1] + scene_bounds[3]) / 2.0
    tolerance = max(100.0, min(scene_bounds[2] - scene_bounds[0], scene_bounds[3] - scene_bounds[1]) * 0.018)
    x_matches = 0
    y_matches = 0
    for x, y in centers:
        if any(abs(other_x - (2.0 * center_x - x)) <= tolerance and abs(other_y - y) <= tolerance for other_x, other_y in centers):
            x_matches += 1
        if any(abs(other_y - (2.0 * center_y - y)) <= tolerance and abs(other_x - x) <= tolerance for other_x, other_y in centers):
            y_matches += 1
    return {
        "reference_count": len(centers),
        "x_mirror_matches": x_matches,
        "y_mirror_matches": y_matches,
        "x_mirror_ratio": round(x_matches / len(centers), 3),
        "y_mirror_ratio": round(y_matches / len(centers), 3),
        "tolerance_world": round(tolerance, 3),
    }


def build_scene_reasoning_context(
    scene: dict[str, Any],
    catalog: dict[str, Any],
    semantic_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded scene context consumed by the primary CAD Agent.

    The context intentionally contains spatial anchors and constraints rather
    than pixels.  It lets the Agent reason about rooms, repetition and
    symmetry when a block is incomplete or its layers are inconsistent, while
    keeping every final object tied to existing native handles.
    """
    scene_bounds = _safe_bbox(scene.get("world_bounds")) or [0.0, 0.0, 0.0, 0.0]
    scene_id = str(scene.get("scene_id") or "")
    references_by_family: dict[str, list[dict[str, Any]]] = {}
    families: list[dict[str, Any]] = []
    for family in catalog.get("families") or []:
        family_id = str(family.get("family_id") or "")
        references = _reference_sample(family, scene_bounds)
        references_by_family[family_id] = references
        geometry = dict(family.get("representative_geometry") or {})
        geometry.pop("bbox_world", None)
        families.append({
            "family_id": family_id,
            "family_kind": family.get("family_kind", "block_reference"),
            "effective_name": family.get("effective_name", ""),
            "block_names": family.get("block_names", []),
            "reference_count_in_scene": len(references),
            "reference_count_total": family.get("reference_count", 0),
            "native_type_counts": family.get("native_type_counts", {}),
            "layer_counts": family.get("layer_counts", {}),
            "space_counts": family.get("space_counts", {}),
            "representative_geometry": geometry,
            "references": references,
            "symmetry": _symmetry_summary(references, scene_bounds),
            "incomplete_signals": [
                signal for signal, enabled in (
                    ("standalone_linework", family.get("family_kind") == "linework_layer"),
                    ("mixed_native_types", len(family.get("native_type_counts") or {}) > 1),
                    ("no_exploded_children", int(geometry.get("child_count", 0) or 0) == 0),
                    ("opaque_or_generated_name", str(family.get("effective_name") or "").lower().startswith(("a$c", "*u", "$"))),
                ) if enabled
            ],
            "evidence_refs": family.get("evidence_refs", []),
        })

    room_anchors: list[dict[str, Any]] = []
    text_hints: list[dict[str, Any]] = []
    for text in scene.get("texts") or []:
        if not isinstance(text, dict):
            continue
        value = str(text.get("normalized_text") or text.get("text") or "").strip()
        if not value:
            continue
        role = str(text.get("role") or "")
        if role == "room_label" or value in _ROOM_CONTEXT_RULES:
            bbox = _safe_bbox(text.get("bbox_world"))
            if bbox is None:
                continue
            rule = next((item for key, item in _ROOM_CONTEXT_RULES.items() if key in value), {})
            room_anchors.append({
                "text_id": text.get("text_id") or text.get("handle"),
                "label": value,
                "bbox_world": bbox,
                "layer": text.get("layer", ""),
                "room_type": rule.get("room_type", "unknown_room"),
                "likely_subtypes": rule.get("likely_subtypes", []),
                "unlikely_subtypes": rule.get("unlikely_subtypes", []),
            })
        if role in {"room_label", "equipment_label"} or any(char in value for char in "卫生间厨房卧室客厅餐厅阳台马桶洁具洗手盆床沙发柜"):
            bbox = _safe_bbox(text.get("bbox_world"))
            if bbox is not None:
                text_hints.append({
                    "text_id": text.get("text_id") or text.get("handle"),
                    "text": value,
                    "role": role,
                    "bbox_world": bbox,
                    "layer": text.get("layer", ""),
                })

    for room in room_anchors:
        room_center = _bbox_center(room["bbox_world"])
        nearby: list[tuple[float, str]] = []
        if room_center is not None:
            for family in families:
                family_centers = [center for ref in family["references"] if (center := _bbox_center(ref.get("bbox_world"))) is not None]
                if not family_centers:
                    continue
                distance = min(math.dist(room_center, center) for center in family_centers)
                nearby.append((distance, str(family["family_id"])))
        room["nearby_family_ids"] = [family_id for _, family_id in sorted(nearby)[:12]]

    native_decisions = {
        str(item.get("family_id")): {
            "type": item.get("type"),
            "subtype": item.get("subtype"),
            "confidence": item.get("confidence"),
            "semantic_source": item.get("semantic_source"),
            "uncertainty_codes": item.get("uncertainty_codes", []),
        }
        for item in (semantic_catalog or {}).get("decisions") or []
        if isinstance(item, dict)
    }
    native_component_decisions = {
        str(item.get("component_id")): {
            "component_id": item.get("component_id"),
            "family_id": item.get("family_id"),
            "type": item.get("type"),
            "subtype": item.get("subtype"),
            "confidence": item.get("confidence"),
            "semantic_source": item.get("semantic_source"),
            "uncertainty_codes": item.get("uncertainty_codes", []),
        }
        for item in (semantic_catalog or {}).get("component_decisions") or []
        if isinstance(item, dict) and item.get("component_id")
    }
    for family in families:
        family["native_agent_prior"] = native_decisions.get(str(family["family_id"]))
        family["native_component_priors"] = [
            native_component_decisions[component.get("component_id")]
            for reference in family.get("references") or []
            for component in reference.get("nested_components") or []
            if component.get("component_id") in native_component_decisions
        ]

    relations = [{
        "relation": "repeated_family",
        "family_id": family["family_id"],
        "reference_count": family["reference_count_in_scene"],
        "symmetry": family["symmetry"],
    } for family in families if family["reference_count_in_scene"] >= 2]
    return {
        "schema_version": "cad_scene_reasoning_context.v1",
        "scene_id": scene_id,
        "world_bounds": scene_bounds,
        "room_anchors": room_anchors,
        "text_hints": text_hints[:240],
        "families": families,
        "spatial_relations": relations,
        "reasoning_constraints": [
            "房间标签是强语境证据，但仍需用墙体边界、门和相邻几何确认归属。",
            "对称或重复只用于补足/校验类别，不得创建不存在的 Handle 或几何。",
            "图层不一致或块展开不完整时，优先联合名称、空间位置、代表几何和同户型重复模式。",
            "卫生间中的洁具候选优先于柜体/床头柜等卧室家具；厨房中的灶具、水槽、冰箱优先于卧室家具。",
            "每个结论必须回指已有 family_id 和 evidence_refs；无法落到原生对象时只能输出待复核假设。",
            "若 nested_block_reference 的 definition_name/effective_name 是明确的器具名称（例如 马桶(730)），将其视为强原生语义证据；对象边界使用该嵌套引用的 primitive_ids，不得把父级卫生间组合块当成马桶。",
        ],
        "block_definition_hints": [
            {
                "definition_name": item.get("definition_name", ""),
                "effective_name": item.get("effective_name", ""),
                "entity_count": item.get("entity_count", 0),
                "model_space_ref_count": item.get("model_space_ref_count", 0),
                "nested_reference_count": item.get("nested_reference_count", 0),
                "nested_block_definitions": list(item.get("nested_block_definitions") or [])[:64],
            }
            for item in (catalog.get("block_definitions") or [])
            if isinstance(item, dict)
            and (item.get("definition_name") or item.get("effective_name"))
            and (
                not bool(item.get("is_anonymous"))
                or item.get("nested_reference_count", 0)
            )
        ][:240],
    }


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", str(text).strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise RuntimeError("CAD reasoning response must be a JSON object")
    parsed.setdefault("decisions", [])
    parsed.setdefault("notes", "")
    return parsed


class DeepSeekCADReasoningProvider:
    """DeepSeek adapter for native CAD family naming, not image analysis."""

    name = "deepseek"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        selected_key = api_key or env_first("DEEPSEEK_API_KEY", "deepseek")
        if not selected_key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置，无法启用 CAD 推理 Agent")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("CAD 推理 Agent 需要安装 openai") from exc
        self.model = model or env_first(
            "DEEPSEEK_CAD_REASONING_MODEL",
            "DEEPSEEK_MODEL",
            default="deepseek-v4-pro",
        )
        try:
            timeout_s = float(env_first("CAD_REASONING_REQUEST_TIMEOUT_S", "VISION_REQUEST_TIMEOUT_S", default="180"))
        except ValueError:
            timeout_s = 180.0
        self._client = OpenAI(
            api_key=selected_key,
            base_url=env_first("DEEPSEEK_BASE_URL", default="https://api.deepseek.com"),
            timeout=max(1.0, timeout_s),
            max_retries=0,
        )
        self.reasoning_effort = env_first("DEEPSEEK_REASONING_EFFORT", default="high").strip()
        self.thinking_mode = env_first("DEEPSEEK_CAD_REASONING_THINKING", default="enabled").strip().lower()

    def reason(
        self,
        catalog: dict[str, Any],
        *,
        scene_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        families = [_model_family(item) for item in catalog.get("families") or []]
        try:
            batch_size = max(1, int(env_first("CAD_REASONING_BATCH_SIZE", default="40")))
        except ValueError:
            batch_size = 40
        decisions: list[dict[str, Any]] = []
        notes: list[str] = []
        for start in range(0, len(families), batch_size):
            batch = families[start:start + batch_size]
            prompt = f"""你是建筑 CAD 原生对象推理 Agent。输入是 AutoCAD 查询工具生成的原生证据组，不是图片。family_kind=block_reference 表示块族；family_kind=linework_layer 表示未归类的独立线/多段线按原生图层聚合。

块族证据：
{json.dumps(batch, ensure_ascii=False, indent=2)}

场景上下文（用于跨对象推理，不改变原生几何）：
{json.dumps(scene_context or {}, ensure_ascii=False, indent=2)}

任务：给每个 family_id 判断 type 和 subtype。type 只能是 wall/window/door/furniture/unknown。栏杆可映射为 type=wall, subtype=railing；看线、线脚等辅助画法只有在证据足以证明其属于墙体/栏杆构件时才归类，否则返回 unknown。

硬规则：
1. 只能判断输入中的 family_id 或 nested_components 中的 component_id；不能创建对象、Handle、坐标或几何。
2. 每个结论必须引用对应 family/component 给出的 evidence_refs；证据不足就返回 unknown。
3. 对 block_reference，reference_count、entity/child 数量本身不能证明单双开门；scale 也不能在不知道块单位时直接当物理尺寸。
4. 二维镜像只看负变换行列式；两个轴都为负不是镜像。
5. 块名、图层、原生类型、代表子几何可以联合推断。图层是弱证据：当 DorLib + 原生 door 一致时，仅位于通用 WINDOW 图层不构成冲突。
6. 对 linework_layer，图层语义、方向分布、长度统计和实体类型可联合判断；只能给组内现有 Handle 定名，不能补画线。
7. 必须结合 room_anchors、reference 的空间位置、spatial_relations 中的重复/对称关系判断；房间语境用于排除不合理类别，例如卫生间不应把明显洁具判为床头柜。
8. 当一个块展开不完整、名称是匿名名或不同对象跨层时，优先检查 nested_components；允许对已有 component_id 提出类别，但必须保留 uncertainty_codes，并且仍只能命名已有 family_id/component_id。
9. rationale 只写一句可复核依据，不输出思维链。

严格返回 JSON：
{{"decisions":[{{"family_id":"family_0000","component_id":"component:18BD:0004","type":"door","subtype":"single_door","confidence":0.8,"evidence_refs":["native_family:family_0000"],"uncertainty_codes":[],"rationale":"简短可核验依据"}}],"notes":"简短批次说明"}}。family_id 和 component_id 至少填写一个；component_id 用于把匿名父块拆成已有叶子组件的语义候选。"""
            request: dict[str, Any] = {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "你是证据约束的 CAD 推理 Agent，只输出 JSON 最终结论。"},
                    {"role": "user", "content": prompt},
                ],
            }
            reasoning_effort = getattr(self, "reasoning_effort", None)
            if reasoning_effort is None:
                reasoning_effort = env_first("DEEPSEEK_REASONING_EFFORT", default="high").strip()
            if reasoning_effort:
                request["reasoning_effort"] = reasoning_effort
            thinking = getattr(self, "thinking_mode", None)
            if thinking is None:
                thinking = env_first("DEEPSEEK_CAD_REASONING_THINKING", default="enabled").strip().lower()
            if thinking in {"enabled", "disabled"}:
                request["extra_body"] = {"thinking": {"type": thinking}}
            response = self._client.chat.completions.create(**request)
            choices = getattr(response, "choices", None) or []
            if not choices:
                raise RuntimeError("CAD reasoning returned no choices")
            content = getattr(getattr(choices[0], "message", None), "content", "")
            parsed = _parse_json(str(content))
            decisions.extend(item for item in parsed.get("decisions") or [] if isinstance(item, dict))
            if parsed.get("notes"):
                notes.append(str(parsed["notes"]))
        return {"decisions": decisions, "notes": " | ".join(notes)}


class OpenAICADReasoningProvider(DeepSeekCADReasoningProvider):
    """OpenAI-compatible adapter for the primary native CAD Agent."""

    name = "openai"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        config = load_openai_compatible_config(model=model, role="cad")
        selected_key = api_key or config.api_key
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("CAD 推理 Agent 需要安装 openai") from exc
        try:
            timeout_s = float(env_first("CAD_REASONING_REQUEST_TIMEOUT_S", "VISION_REQUEST_TIMEOUT_S", default="180"))
        except ValueError:
            timeout_s = 180.0
        self.model = config.model
        self.reasoning_effort = config.reasoning_effort or "low"
        # GPT models do not use DeepSeek's provider-specific thinking body.
        self.thinking_mode = None
        self._client = OpenAI(
            api_key=selected_key,
            base_url=config.base_url,
            timeout=max(1.0, timeout_s),
            max_retries=0,
        )


def _baseline_decisions(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for family in catalog.get("families") or []:
        if family.get("family_kind") == "linework_layer":
            layer_text = " ".join(str(item) for item in (family.get("layer_counts") or {})).lower()
            linework_type = ""
            linework_subtype = ""
            confidence = 0.0
            uncertainty_codes: list[str] = []
            rationale = ""
            if any(term in layer_text for term in ("栏杆", "railing", "handrail", "guardrail")):
                # The public graph currently exposes railing under the wall
                # family, matching SymPoint class 34 -> wall/railing.
                linework_type = "wall"
                linework_subtype = "railing"
                confidence = 0.92
                rationale = "原生图层明确标记为栏杆，按公共 Scene Graph 映射为 wall/railing"
            elif any(term in layer_text for term in ("线脚", "molding", "moulding", "skirting")):
                linework_type = "wall"
                linework_subtype = "wall_detail"
                confidence = 0.72
                uncertainty_codes = ["architectural_detail_not_primary_wall"]
                rationale = "原生图层标记为线脚，仅作为墙体构造细部候选并保留复核"
            if linework_type:
                decisions[str(family["family_id"])] = {
                    "family_id": str(family["family_id"]),
                    "type": linework_type,
                    "subtype": linework_subtype,
                    "confidence": confidence,
                    "evidence_refs": [f"native_family:{family['family_id']}"],
                    "uncertainty_codes": uncertainty_codes,
                    "rationale": rationale,
                    "semantic_source": "native_layer_semantics",
                }
            continue
        known_types = {
            str(value) for value, count in (family.get("native_type_counts") or {}).items()
            if str(value) in CAD_OBJECT_TYPES - {"unknown"} and int(count or 0) > 0
        }
        if len(known_types) != 1:
            continue
        object_type = next(iter(known_types))
        decisions[str(family["family_id"])] = {
            "family_id": str(family["family_id"]),
            "type": object_type,
            "subtype": str(family.get("effective_name") or object_type),
            "confidence": 0.86,
            "evidence_refs": [f"native_family:{family['family_id']}"],
            "uncertainty_codes": [],
            "rationale": "AutoCAD 原生分类在该块族内一致",
            "semantic_source": "native_cad_metadata",
        }
    return decisions


def _named_nested_component_semantics(component: dict[str, Any]) -> tuple[str, str] | None:
    """Map an explicit nested block definition name to a safe subtype.

    This is intentionally narrow.  It is not a visual classifier and does not
    infer from shape; it only honors names that AutoCAD exposes in its
    BlockTable.  Such a name is stronger than a weak SYP class collision.
    """
    value = " ".join(
        str(component.get(key) or "")
        for key in ("definition_name", "effective_name", "block_name")
    ).casefold()
    mappings: tuple[tuple[tuple[str, ...], str], ...] = (
        (("壁挂坐便", "壁挂马桶", "wall hung toilet", "wall-hung toilet"), "wall_hung_toilet"),
        (("马桶", "坐便器", "坐便", "toilet", "wc"), "toilet"),
        (("蹲便", "蹲厕", "squat toilet"), "squat_toilet"),
        (("小便器", "小便斗", "urinal"), "urinal"),
        (("洗手盆", "洗脸盆", "台盆", "sink", "basin"), "sink"),
        (("浴缸", "bathtub", "bath tub"), "bath_tub"),
        (("淋浴", "shower"), "shower"),
        (("洗衣机", "washing machine", "washing_machine"), "washing_machine"),
    )
    for terms, subtype in mappings:
        if any(term.casefold() in value for term in terms):
            return "furniture", subtype
    return None


def apply_cad_reasoning(
    catalog: dict[str, Any],
    response: dict[str, Any] | None,
    *,
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Validate model decisions and combine them with deterministic metadata."""

    family_map = {str(item["family_id"]): item for item in catalog.get("families") or []}
    component_map: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for family in catalog.get("families") or []:
        family_id = str(family.get("family_id") or "")
        for reference in family.get("references") or []:
            for component in reference.get("nested_components") or []:
                component_id = str(component.get("component_id") or "")
                if component_id:
                    component_map[component_id] = (family, component)
    decisions = _baseline_decisions(catalog)
    component_decisions: dict[str, dict[str, Any]] = {}
    for family in catalog.get("families") or []:
        for reference in family.get("references") or []:
            for component in reference.get("nested_components") or []:
                component_id = str(component.get("component_id") or "")
                if not component_id or component.get("component_kind") != "nested_block_reference":
                    continue
                semantics = _named_nested_component_semantics(component)
                if semantics is None:
                    continue
                object_type, subtype = semantics
                component_decisions[component_id] = {
                    "family_id": str(family.get("family_id") or ""),
                    "component_id": component_id,
                    "type": object_type,
                    "subtype": subtype,
                    "confidence": 0.97,
                    "evidence_refs": list(component.get("evidence_refs") or [])[:8],
                    "uncertainty_codes": [],
                    "rationale": f"AutoCAD BlockTable 嵌套定义名明确为 {component.get('definition_name') or component.get('block_name')}",
                    "semantic_source": "native_named_block_definition",
                }
    rejected: list[dict[str, Any]] = []
    for decision in (response or {}).get("decisions") or []:
        if not isinstance(decision, dict):
            rejected.append({"reason": "decision_not_object"})
            continue
        component_id = str(decision.get("component_id") or "")
        family_id = str(decision.get("family_id") or "")
        component = None
        if component_id:
            component_entry = component_map.get(component_id)
            if component_entry is None:
                rejected.append({"component_id": component_id, "reason": "unknown_component_id"})
                continue
            family, component = component_entry
            family_id = str(family.get("family_id") or family_id)
        else:
            family = family_map.get(family_id)
        if family is None:
            rejected.append({"family_id": family_id, "component_id": component_id, "reason": "unknown_family_id"})
            continue
        if _GEOMETRY_MUTATION_KEYS.intersection(decision):
            rejected.append({"family_id": family_id, "component_id": component_id, "reason": "geometry_mutation_forbidden"})
            continue
        object_type = str(decision.get("type") or "unknown").strip().lower()
        subtype = str(decision.get("subtype") or "unknown").strip()
        if object_type not in CAD_OBJECT_TYPES or not subtype:
            rejected.append({"family_id": family_id, "component_id": component_id, "reason": "invalid_type_or_subtype"})
            continue
        valid_refs = set(family.get("evidence_refs") or [])
        for reference in family.get("references") or []:
            valid_refs.update(reference.get("evidence_refs") or [])
            for nested_component in reference.get("nested_components") or []:
                if str(nested_component.get("component_id") or "") == component_id:
                    valid_refs.update(nested_component.get("evidence_refs") or [])
        evidence_refs = [str(item) for item in decision.get("evidence_refs") or []]
        if not evidence_refs or any(item not in valid_refs for item in evidence_refs):
            rejected.append({"family_id": family_id, "component_id": component_id, "reason": "missing_or_unknown_evidence_refs"})
            continue
        try:
            confidence = max(0.0, min(1.0, float(decision.get("confidence", 0.0) or 0.0)))
        except (TypeError, ValueError):
            rejected.append({"family_id": family_id, "component_id": component_id, "reason": "invalid_confidence"})
            continue

        native_types = {
            name for name, count in (family.get("native_type_counts") or {}).items()
            if name in CAD_OBJECT_TYPES - {"unknown"} and int(count or 0) > 0
        }
        geometry_count = int((component or {}).get("primitive_count") or 0) if component is not None else int((family.get("representative_geometry") or {}).get("child_count") or 0)
        if len(native_types) == 1 and object_type in native_types:
            confidence_cap = 0.95
        elif geometry_count > 0 and int(family.get("reference_count") or 0) >= 2:
            confidence_cap = 0.88
        elif geometry_count > 0 or family.get("effective_name") or family.get("layer_counts"):
            confidence_cap = 0.80
        else:
            confidence_cap = 0.65
        confidence = min(confidence, confidence_cap)

        uncertainty_codes = [str(item) for item in decision.get("uncertainty_codes") or []]
        baseline = None if component_id else decisions.get(family_id)
        effective_name = str(family.get("effective_name") or "").lower()
        if (
            "layer_native_type_conflict" in uncertainty_codes
            and len(native_types) == 1
            and object_type in native_types
            and (
                (object_type == "door" and "dorlib" in effective_name)
                or (object_type == "window" and "winlib" in effective_name)
            )
        ):
            # Door/window libraries in real drawings are frequently stored on
            # one shared WINDOW layer.  A weak generic layer must not turn a
            # block-name + native-type agreement into 44 false review items.
            uncertainty_codes = [
                code for code in uncertainty_codes if code != "layer_native_type_conflict"
            ]
        if baseline and object_type not in {"unknown", baseline["type"]}:
            uncertainty_codes.append("MODEL_NATIVE_TYPE_CONFLICT")
            object_type = baseline["type"]
            confidence = min(confidence, float(baseline["confidence"]), 0.70)
        if object_type == "unknown" and baseline:
            continue
        reasoning_source = f"{provider}_cad_reasoning" if provider else "cad_reasoning"
        normalized = {
            "family_id": family_id,
            **({"component_id": component_id} if component_id else {}),
            "type": object_type,
            "subtype": subtype,
            "confidence": round(confidence, 4),
            "evidence_refs": evidence_refs,
            "uncertainty_codes": list(dict.fromkeys(uncertainty_codes)),
            "rationale": str(decision.get("rationale") or "")[:500],
            "semantic_source": reasoning_source,
        }
        if component_id:
            component_decisions[component_id] = normalized
        else:
            decisions[family_id] = normalized

    return {
        "schema_version": "native_semantic_catalog.v1",
        "provider": {"name": provider or None, "model": model or None},
        "decisions": [decisions[key] for key in sorted(decisions)],
        "component_decisions": [component_decisions[key] for key in sorted(component_decisions)],
        "rejected": rejected,
        "notes": str((response or {}).get("notes") or "")[:1000],
        "summary": {
            "family_count": len(family_map),
            "classified_family_count": sum(1 for item in decisions.values() if item["type"] != "unknown"),
            "model_decision_count": sum(
                1
                for item in [*decisions.values(), *component_decisions.values()]
                if item["semantic_source"] == (f"{provider}_cad_reasoning" if provider else "cad_reasoning")
            ),
            "component_decision_count": len(component_decisions),
            "rejected_decision_count": len(rejected),
        },
    }


def _intersects(first: list[float], second: list[float]) -> bool:
    return not (first[2] < second[0] or first[0] > second[2] or first[3] < second[1] or first[1] > second[3])


def native_candidates_for_scene(
    scene: dict[str, Any],
    catalog: dict[str, Any],
    semantic_catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expand native decisions and preserve known Scene geometry.

    Family decisions cover BlockReferences and standalone unknown linework.  A
    Scene can also contain already-classified native primitives (for example,
    WALL-layer lines) that do not belong to either catalog family.  Keep those
    primitives as geometry-only native candidates when no family candidate has
    claimed them; remote SymPoint inference must not be allowed to erase them
    merely because it returned no ``wall`` instance.
    """

    decisions = {str(item.get("family_id")): item for item in semantic_catalog.get("decisions") or []}
    component_decisions = {
        str(item.get("component_id")): item
        for item in semantic_catalog.get("component_decisions") or []
        if isinstance(item, dict) and item.get("component_id")
    }
    primitive_roots: dict[str, list[str]] = defaultdict(list)
    for primitive in scene.get("primitives") or []:
        source = primitive.get("source") or {}
        root = str(source.get("exploded_from") or source.get("native_root_handle") or "")
        if not root:
            source_id = str(primitive.get("source_entity_id") or "")
            root = source_id.split(":explode:", 1)[0] if ":explode:" in source_id else source_id
        if root:
            primitive_roots[root].append(str(primitive.get("primitive_id") or ""))

    scene_bounds = require_bbox(scene.get("world_bounds"), "scene.world_bounds")
    candidates: list[dict[str, Any]] = []
    for family in catalog.get("families") or []:
        decision = decisions.get(str(family.get("family_id")))
        if not decision or decision.get("type") not in CAD_OBJECT_TYPES - {"unknown"}:
            continue
        for reference in family.get("references") or []:
            exact_nested_components = [
                component for component in reference.get("nested_components") or []
                if component.get("component_kind") == "nested_block_reference"
                and str(component.get("component_id") or "") in component_decisions
                and component_decisions[str(component.get("component_id") or "")].get("type") in CAD_OBJECT_TYPES - {"unknown"}
            ]
            if exact_nested_components:
                # A composite parent is only a container here.  Once an
                # explicit named child has a semantic decision, do not emit
                # the parent's all-in-one bbox as a competing furniture/door
                # object; the child component owns its exact primitives.
                continue
            bbox = _safe_bbox(reference.get("bbox_world"))
            if bbox is None or not _intersects(scene_bounds, bbox):
                continue
            handle = str(reference.get("handle") or reference.get("entity_id") or "")
            primitive_ids = [item for item in primitive_roots.get(handle, []) if item]
            uncertainty = list(decision.get("uncertainty_codes") or [])
            blocking_uncertainty = [
                code for code in uncertainty
                if code not in {"empty_representative_geometry"}
            ]
            evidence_prefix = (
                "native_linework"
                if family.get("family_kind") == "linework_layer"
                else "native_block"
            )
            candidates.append({
                "candidate_id": f"native:{handle}",
                "family_id": str(family.get("family_id") or ""),
                "handle": handle,
                "type": str(decision["type"]),
                "subtype": str(decision.get("subtype") or decision["type"]),
                "bbox_world": bbox,
                "primitive_ids": primitive_ids,
                "confidence": float(decision.get("confidence", 0.0) or 0.0),
                "status": "review" if blocking_uncertainty else "confirmed",
                "semantic_source": str(decision.get("semantic_source") or "cad_reasoning"),
                "evidence_refs": list(dict.fromkeys([
                    *list(decision.get("evidence_refs") or []),
                    f"{evidence_prefix}:{handle}",
                ])),
                "evidence": {
                    "effective_name": family.get("effective_name", ""),
                    "layer": reference.get("layer", ""),
                    "rotation_deg": reference.get("rotation_deg", 0.0),
                    "scale": reference.get("scale", {}),
                    "is_reflected_2d": reference.get("is_reflected_2d", False),
                    "geometry_summary": reference.get("geometry_summary", {}),
                    "rationale": decision.get("rationale", ""),
                    "uncertainty_codes": uncertainty,
                },
            })

    covered_primitive_ids = {
        str(primitive_id)
        for candidate in candidates
        for primitive_id in candidate.get("primitive_ids") or []
    }
    # Component-level CAD Agent decisions are the open-world primary path for
    # anonymous/composite parents.  Resolve them back to the exact Scene
    # primitive IDs; never use a model-supplied bbox as geometry authority.
    for family in catalog.get("families") or []:
        family_decision = decisions.get(str(family.get("family_id") or "")) or {}
        for reference in family.get("references") or []:
            for component in reference.get("nested_components") or []:
                component_id = str(component.get("component_id") or "")
                decision = component_decisions.get(component_id)
                if not decision or decision.get("type") not in CAD_OBJECT_TYPES - {"unknown"}:
                    continue
                is_exact_nested_reference = component.get("component_kind") == "nested_block_reference"
                if family_decision.get("type") not in {None, "unknown", ""} and not is_exact_nested_reference:
                    # A known family normally owns its native boundary. An
                    # explicitly named nested BlockReference is the one
                    # exception: it is a smaller authoritative object inside
                    # a composite parent and must be allowed to split it.
                    continue
                source_entity_ids = {str(item) for item in component.get("source_entity_ids") or [] if item}
                component_meta = {
                    "native_root_handle": str(component.get("native_root_handle") or reference.get("handle") or ""),
                    "block_name": str(component.get("block_name") or ""),
                    "parent_path": str(component.get("parent_path") or ""),
                }
                primitive_ids: list[str] = []
                for primitive in scene.get("primitives") or []:
                    primitive_source = primitive.get("source") or {}
                    primitive_source_id = str(primitive.get("source_entity_id") or "")
                    exact_match = primitive_source_id in source_entity_ids
                    provenance_match = (
                        str(primitive_source.get("handle") or "") == component_meta["native_root_handle"]
                        and str(primitive_source.get("block_name") or "") == component_meta["block_name"]
                        and str(primitive_source.get("exploded_from") or "") == component_meta["parent_path"]
                    )
                    if (exact_match or provenance_match) and primitive.get("primitive_id"):
                        primitive_ids.append(str(primitive["primitive_id"]))
                primitive_ids = sorted(set(primitive_ids))
                if not primitive_ids or set(primitive_ids).intersection(covered_primitive_ids):
                    continue
                bbox = _safe_bbox((component.get("geometry_summary") or {}).get("bbox_world"))
                if bbox is None:
                    bbox = _bbox_union([
                        primitive.get("bbox_world")
                        for primitive in scene.get("primitives") or []
                        if str(primitive.get("primitive_id")) in set(primitive_ids)
                    ])
                if bbox is None:
                    continue
                uncertainty = list(decision.get("uncertainty_codes") or [])
                candidates.append({
                    "candidate_id": f"native:component:{component_id}",
                    "family_id": str(family.get("family_id") or ""),
                    "component_id": component_id,
                    "handle": component_meta["native_root_handle"],
                    "type": str(decision["type"]),
                    "subtype": str(decision.get("subtype") or decision["type"]),
                    "bbox_world": bbox,
                    "primitive_ids": primitive_ids,
                    "confidence": float(decision.get("confidence", 0.0) or 0.0),
                    "status": "review" if uncertainty else "confirmed",
                    "semantic_source": str(decision.get("semantic_source") or "cad_reasoning_component"),
                    "evidence_refs": list(dict.fromkeys([
                        *list(decision.get("evidence_refs") or []),
                        *list(component.get("evidence_refs") or []),
                    ])),
                    "evidence": {
                        "component_id": component_id,
                        "parent_family_id": str(family.get("family_id") or ""),
                        "block_name": component_meta["block_name"],
                        "parent_path": component_meta["parent_path"],
                        "geometry_summary": component.get("geometry_summary", {}),
                        "rationale": decision.get("rationale", ""),
                        "uncertainty_codes": uncertainty,
                        "candidate_boundary": "native_leaf_component",
                        "component_kind": component.get("component_kind", "legacy_exploded_group"),
                        "definition_name": component.get("definition_name") or component.get("block_name") or "",
                        "reference_id": component.get("reference_id") or "",
                    },
                })
                covered_primitive_ids.update(primitive_ids)
    for primitive in scene.get("primitives") or []:
        primitive_id = str(primitive.get("primitive_id") or "")
        entity_type = str(primitive.get("type") or "").lower()
        if not primitive_id or primitive_id in covered_primitive_ids or entity_type not in CAD_OBJECT_TYPES - {"unknown"}:
            continue
        bbox = _safe_bbox(primitive.get("bbox_world"))
        if bbox is None or not _intersects(scene_bounds, bbox):
            continue
        source = primitive.get("source") or {}
        handle = str(primitive.get("handle") or primitive.get("source_entity_id") or "")
        source_entity_id = str(primitive.get("source_entity_id") or handle)
        candidates.append({
            "candidate_id": f"native:primitive:{primitive_id}",
            "family_id": "",
            "handle": handle,
            "type": entity_type,
            "subtype": entity_type,
            "bbox_world": bbox,
            "primitive_ids": [primitive_id],
            "confidence": 0.82,
            "status": "confirmed",
            "semantic_source": "native_geometry_type",
            "evidence_refs": list(dict.fromkeys([
                f"native_geometry:{source_entity_id or primitive_id}",
                f"native_primitive:{primitive_id}",
            ])),
            "evidence": {
                "layer": str(primitive.get("layer") or source.get("layer") or ""),
                "entity_type": str(primitive.get("entity_type") or primitive.get("subtype") or ""),
                "command": str(primitive.get("command") or ""),
                "length": primitive.get("length"),
                "rotation_deg": primitive.get("rotation_deg", 0.0),
                "source": "scene_native_type_fallback",
            },
        })
        covered_primitive_ids.add(primitive_id)
    return candidates
