from __future__ import annotations

"""Reconcile overlapping panels and multiple model opinions."""

import math
from collections import defaultdict
from typing import Any, Iterable

from .schema import normalize_entity


def area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def iou(first: list[float], second: list[float]) -> float:
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = area([left, top, right, bottom])
    union = area(first) + area(second) - intersection
    return intersection / union if union else 0.0


def _center(box: list[float]) -> tuple[float, float]:
    return (float(box[0] + box[2]) / 2.0, float(box[1] + box[3]) / 2.0)


def _same_object(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if first["type"] != second["type"]:
        return False
    first_box, second_box = first["bbox_px"], second["bbox_px"]
    threshold = 0.22 if first["type"] == "wall" else 0.30
    if iou(first_box, second_box) >= threshold:
        return True
    first_center, second_center = _center(first_box), _center(second_box)
    distance = ((first_center[0] - second_center[0]) ** 2 + (first_center[1] - second_center[1]) ** 2) ** 0.5
    scale = max(1.0, min(first_box[2] - first_box[0], first_box[3] - first_box[1]))
    return distance <= scale * 0.28


def panel_to_global_bbox(local_bbox: Iterable[float], panel: dict[str, Any], page: dict[str, Any]) -> list[float]:
    values = [float(value) for value in local_bbox]
    if len(values) != 4:
        raise ValueError("bbox_px must contain four numbers")
    scale = float(panel.get("scale", 1.0) or 1.0)
    left, top, right, bottom = values
    result = [
        float(panel["global_x"]) + left / scale,
        float(panel["global_y"]) + top / scale,
        float(panel["global_x"]) + right / scale,
        float(panel["global_y"]) + bottom / scale,
    ]
    result[0] = max(0.0, min(float(page["width"]), result[0]))
    result[1] = max(0.0, min(float(page["height"]), result[1]))
    result[2] = max(0.0, min(float(page["width"]), result[2]))
    result[3] = max(0.0, min(float(page["height"]), result[3]))
    return result


def _map_candidate(candidate: dict[str, Any], panel: dict[str, Any], page: dict[str, Any], provider: str, model: str, mosaic_id: str) -> dict[str, Any]:
    scale = float(panel.get("scale", 1.0) or 1.0)
    polygon = [
        [
            float(panel["global_x"]) + float(point[0]) / scale,
            float(panel["global_y"]) + float(point[1]) / scale,
        ]
        for point in (candidate.get("polygon_px") or [])
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    entity = normalize_entity(
        {
            **candidate,
            "bbox_px": panel_to_global_bbox(candidate.get("bbox_px", [0, 0, 0, 0]), panel, page),
            "polygon_px": polygon,
            "evidence": {
                "visual_summary": str(candidate.get("evidence") or ""),
                "nearby_text": list(candidate.get("nearby_text") or []),
                "provider": provider,
                "model": model,
            },
            "provenance": {
                "source": "vision",
                "provider": provider,
                "model": model,
                "mosaic_id": mosaic_id,
                "panel_id": panel["panel_id"],
                "bbox_panel_px": candidate.get("bbox_px", []),
            },
        },
        default_source="vision",
    )
    entity["coordinate_space"] = "pixel"
    return entity


def collect_visual_candidates(
    *,
    response: dict[str, Any],
    mosaic: dict[str, Any],
    page: dict[str, Any],
    provider: str,
    model: str,
) -> list[dict[str, Any]]:
    panels = {panel["panel_id"]: panel for panel in mosaic.get("panels", [])}
    candidates: list[dict[str, Any]] = []
    for raw in response.get("detections", []):
        panel = panels.get(raw.get("panel_id"))
        if panel is None:
            continue
        try:
            entity = _map_candidate(raw, panel, page, provider, model, mosaic["mosaic_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if area(entity["bbox_px"]) <= 0:
            continue
        candidates.append(entity)
    return candidates


def reconcile_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cluster duplicate panel/model detections and retain vote provenance."""
    clusters: list[list[dict[str, Any]]] = []
    # A drawing can contain thousands of vector entities.  A spatial index
    # keeps reconciliation close to linear instead of comparing every wall
    # segment with every other segment.
    bucket_size = 512.0
    buckets: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    for candidate in candidates:
        box = candidate.get("bbox_px", [0, 0, 0, 0])
        center = _center(box)
        key_type = str(candidate.get("type", ""))
        cell_x, cell_y = math.floor(center[0] / bucket_size), math.floor(center[1] / bucket_size)
        possible_indices: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                possible_indices.extend(buckets.get((key_type, cell_x + dx, cell_y + dy), []))
        for cluster_index in possible_indices:
            cluster = clusters[cluster_index]
            if _same_object(candidate, cluster[0]):
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])
            buckets[(key_type, cell_x, cell_y)].append(len(clusters) - 1)

    results: list[dict[str, Any]] = []
    for cluster in clusters:
        representative = max(cluster, key=lambda item: item["confidence"])
        total_weight = sum(max(0.01, item["confidence"]) for item in cluster)
        bbox = [
            sum(item["bbox_px"][index] * max(0.01, item["confidence"]) for item in cluster) / total_weight
            for index in range(4)
        ]
        providers = sorted({item["provenance"].get("provider", "unknown") for item in cluster})
        models = sorted({item["provenance"].get("model", "unknown") for item in cluster})
        support = len(cluster)
        confidence = min(0.99, max(item["confidence"] for item in cluster) + min(0.20, 0.08 * (support - 1)))
        result = dict(representative)
        result["bbox_px"] = bbox
        result["confidence"] = round(confidence, 4)
        result["evidence"] = {
            **representative.get("evidence", {}),
            "model_support": support,
            "providers": providers,
            "models": models,
            "agreement": "multi-model_or_overlap" if support > 1 else "single_observation",
        }
        result["provenance"] = {
            **representative.get("provenance", {}),
            "observations": [item.get("provenance", {}) for item in cluster],
        }
        results.append(result)
    return sorted(results, key=lambda item: (item["type"], item["bbox_px"][1], item["bbox_px"][0]))
