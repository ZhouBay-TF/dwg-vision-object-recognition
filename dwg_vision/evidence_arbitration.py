from __future__ import annotations

"""Evidence arbitration for semantic labels.

No model is treated as a universal oracle here.  Text labels are a hard
semantic instruction because the product requirement is to follow explicit
equipment text.  Native geometry can confirm coarse structural types.  SYP,
DeepSeek and visual review are independent semantic observations; without an
agreement or a hard label they remain provisional and require review.
"""

from collections import defaultdict
from typing import Any, Iterable


GENERIC_SUBTYPES = frozenset({"", "unknown"})

# These are conservative policy priors, not claimed model accuracies.  A gold
# evaluation can provide a per-source/per-class accuracy factor at runtime;
# when it is absent, the agreement rule below prevents one model score from
# becoming a false confirmation.
SOURCE_POLICIES: dict[str, dict[str, Any]] = {
    "native_text": {
        "tier": "authoritative",
        "weight": 1.00,
        "scope": "open_semantic_label",
        "requires_agreement": False,
        "independence_group": "native_text",
    },
    "native_geometry_type": {
        "tier": "structural",
        "weight": 0.92,
        "scope": "coarse_cad_type",
        "requires_agreement": False,
        "independence_group": "native_cad",
    },
    "native_cad_metadata": {
        "tier": "structural",
        "weight": 0.86,
        "scope": "native_cad_metadata",
        "requires_agreement": True,
        "independence_group": "native_cad",
    },
    "deepseek_cad_reasoning": {
        "tier": "semantic_model",
        "weight": 0.64,
        "scope": "open_semantic_hypothesis",
        "requires_agreement": True,
        "independence_group": "deepseek",
    },
    "openai_cad_reasoning": {
        "tier": "semantic_model",
        "weight": 0.64,
        "scope": "open_semantic_hypothesis",
        "requires_agreement": True,
        "independence_group": "openai",
    },
    "cad_reasoning": {
        "tier": "semantic_model",
        "weight": 0.60,
        "scope": "open_semantic_hypothesis",
        "requires_agreement": True,
        "independence_group": "deepseek",
    },
    "sympointv2": {
        "tier": "closed_set_model",
        "weight": 0.64,
        "scope": "closed_set_35_classes",
        "requires_agreement": True,
        "independence_group": "sympointv2",
    },
    "sympointv2_primitive": {
        "tier": "closed_set_model",
        "weight": 0.52,
        "scope": "closed_set_35_classes",
        "requires_agreement": True,
        "independence_group": "sympointv2",
    },
    "visual_gap_supplement": {
        "tier": "visual_model",
        "weight": 0.50,
        "scope": "image_hypothesis",
        "requires_agreement": True,
        "independence_group": "visual",
    },
    "visual_review": {
        "tier": "visual_model",
        "weight": 0.50,
        "scope": "image_hypothesis",
        "requires_agreement": True,
        "independence_group": "visual",
    },
    "llm_fusion": {
        "tier": "semantic_model",
        "weight": 0.60,
        "scope": "open_semantic_hypothesis",
        "requires_agreement": True,
        "independence_group": "deepseek",
    },
    "openai_fusion": {
        "tier": "semantic_model",
        "weight": 0.60,
        "scope": "open_semantic_hypothesis",
        "requires_agreement": True,
        "independence_group": "openai",
    },
    "openai_final_agent": {
        "tier": "semantic_model",
        "weight": 0.60,
        "scope": "open_semantic_hypothesis",
        "requires_agreement": True,
        "independence_group": "openai",
    },
}


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _label(value: Any) -> str:
    return str(value or "").strip().lower()


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        label = _label(value)
        if not label or label in seen:
            continue
        seen.add(label)
        result.append(label)
    return result


def _accuracy_factor(
    source: str,
    subtype: str,
    source_calibration: dict[str, Any] | None,
) -> tuple[float, dict[str, Any]]:
    """Return a measured accuracy factor when a sufficiently sized gold row exists."""
    source = str(source or "")
    subtype = _label(subtype)
    calibration = source_calibration or {}
    source_row = calibration.get(source) or {}
    by_subtype = source_row.get("by_subtype") or {}
    if by_subtype:
        measured = by_subtype.get(subtype)
        if measured is None:
            return 1.0, {
                "status": "class_not_measured",
                "source": source,
                "subtype": subtype,
            }
    else:
        measured = source_row.get("overall")
    if not isinstance(measured, dict):
        return 1.0, {"status": "not_measured", "source": source, "subtype": subtype}
    sample_count = int(measured.get("sample_count", 0) or 0)
    accuracy = _clamp(measured.get("accuracy"), default=-1.0)
    if sample_count < 10 or accuracy < 0.0:
        return 1.0, {
            "status": "insufficient_sample",
            "source": source,
            "subtype": subtype,
            "sample_count": sample_count,
        }
    # Do not let a small or single-drawing gold set become absolute truth.
    return max(0.25, accuracy), {
        "status": "measured",
        "source": source,
        "subtype": subtype,
        "sample_count": sample_count,
        "accuracy": accuracy,
        "basis": measured.get("basis", "gold_evaluation"),
    }


def arbitrate_semantic_evidence(
    candidates: Iterable[dict[str, Any]],
    *,
    text_subtypes: Iterable[str] = (),
    fallback_subtype: str = "unknown",
    fallback_source: str = "",
    source_calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose a semantic label while preserving uncertainty.

    A source contributes at most once per label, so overlapping SYP tiles do
    not manufacture confidence by repeating the same observation.  A label
    is confirmed without text only when it has two independent sources, or a
    native structural source that is explicitly limited to a coarse CAD type.
    A conflict always remains ``review`` even when one label has the highest
    weighted support.
    """
    normalized: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        subtype = _label(candidate.get("subtype") or candidate.get("label"))
        source = str(candidate.get("source") or "unknown_source")
        if subtype in GENERIC_SUBTYPES:
            continue
        policy = dict(SOURCE_POLICIES.get(source) or {
            "tier": "unknown_source",
            "weight": 0.40,
            "scope": "unmeasured",
            "requires_agreement": True,
            "independence_group": source,
        })
        accuracy_factor, calibration = _accuracy_factor(source, subtype, source_calibration)
        confidence = _clamp(candidate.get("confidence"), default=0.0)
        normalized.append({
            "subtype": subtype,
            "source": source,
            "confidence": confidence,
            "weighted_support": confidence * float(policy.get("weight", 0.40)) * accuracy_factor,
            "policy": policy,
            "calibration": calibration,
            "order": index,
            "reference": candidate.get("reference"),
        })

    direct_text = _unique(text_subtypes)
    all_labels = _unique(item["subtype"] for item in normalized)
    if direct_text:
        chosen = direct_text[0]
        conflicts = [
            {
                "code": "TEXT_AUTHORITY_CONFLICT",
                "text_subtype": chosen,
                "other_subtype": item["subtype"],
                "source": item["source"],
            }
            for item in normalized
            if item["subtype"] != chosen
        ]
        return {
            "chosen_subtype": chosen,
            "selected_source": "native_text",
            "status": "review" if conflicts else "confirmed",
            "verification_state": "text_authoritative_conflict" if conflicts else "text_authoritative",
            "confidence": 0.99,
            "independent_source_count": len({
                str(item["policy"].get("independence_group") or item["source"])
                for item in normalized
                if item["subtype"] == chosen
            }),
            "independent_source_groups": sorted({
                str(item["policy"].get("independence_group") or item["source"])
                for item in normalized
                if item["subtype"] == chosen
            }),
            "candidate_count": len(normalized),
            "candidate_labels": all_labels,
            "candidate_evidence": normalized,
            "conflicts": conflicts,
            "scope_notes": ["直接设备文字是硬语义证据；其它来源只能保留为冲突审计。"],
            "requires_verification": bool(conflicts),
        }

    if not normalized:
        chosen = _label(fallback_subtype) or "unknown"
        return {
            "chosen_subtype": chosen,
            "selected_source": fallback_source or "unresolved",
            "status": "review",
            "verification_state": "unresolved_no_semantic_evidence",
            "confidence": 0.0,
            "independent_source_count": 0,
            "candidate_count": 0,
            "candidate_labels": [],
            "candidate_evidence": [],
            "conflicts": [],
            "scope_notes": [],
            "requires_verification": True,
        }

    per_source_label: dict[tuple[str, str], dict[str, Any]] = {}
    for item in normalized:
        key = (item["source"], item["subtype"])
        previous = per_source_label.get(key)
        if previous is None or item["weighted_support"] > previous["weighted_support"]:
            per_source_label[key] = item
    label_support: dict[str, float] = defaultdict(float)
    label_sources: dict[str, set[str]] = defaultdict(set)
    label_groups: dict[str, set[str]] = defaultdict(set)
    label_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    first_order: dict[str, int] = {}
    for (source, subtype), item in per_source_label.items():
        label_support[subtype] += item["weighted_support"]
        label_sources[subtype].add(source)
        label_groups[subtype].add(str(item["policy"].get("independence_group") or source))
        label_rows[subtype].append(item)
        first_order[subtype] = min(first_order.get(subtype, item["order"]), item["order"])
    chosen = max(label_support, key=lambda value: (label_support[value], -first_order[value]))
    chosen_rows = label_rows[chosen]
    chosen_source = max(chosen_rows, key=lambda item: (item["weighted_support"], -item["order"]))["source"]
    other_labels = [label for label in label_support if label != chosen]
    conflicts = [
        {
            "code": "SEMANTIC_EVIDENCE_CONFLICT",
            "chosen_subtype": chosen,
            "other_subtype": other,
            "chosen_sources": sorted(label_sources[chosen]),
            "other_sources": sorted(label_sources[other]),
        }
        for other in sorted(other_labels)
    ]
    structural_confirmation = any(
        item["subtype"] == chosen
        and item["policy"].get("tier") == "structural"
        and not item["policy"].get("requires_agreement", True)
        for item in chosen_rows
    )
    independent_count = len(label_groups[chosen])
    if conflicts:
        status = "review"
        verification_state = "conflict_review_weighted_candidate"
    elif structural_confirmation:
        status = "confirmed"
        verification_state = "native_structural_confirmed"
    elif independent_count >= 2:
        status = "confirmed"
        verification_state = "multi_source_confirmed"
    else:
        status = "review"
        verification_state = "single_source_provisional"
    confidence = max(item["confidence"] for item in chosen_rows)
    scope_notes: list[str] = []
    if any(item["policy"].get("scope") == "closed_set_35_classes" for item in normalized):
        scope_notes.append("SYP 只覆盖 35 个闭集类别；未返回某类别不能作为该对象不是该类别的反证。")
    if any(item["policy"].get("tier") == "semantic_model" for item in normalized):
        scope_notes.append("LLM 推理是开放类别语义假设，未有独立证据时只能作为 provisional。")
    return {
        "chosen_subtype": chosen,
        "selected_source": chosen_source,
        "status": status,
        "verification_state": verification_state,
        "confidence": confidence,
        "independent_source_count": independent_count,
        "independent_source_groups": sorted(label_groups[chosen]),
        "candidate_count": len(normalized),
        "candidate_labels": all_labels,
        "candidate_evidence": normalized,
        "label_support": {label: round(value, 6) for label, value in sorted(label_support.items())},
        "conflicts": conflicts,
        "scope_notes": scope_notes,
        "requires_verification": status != "confirmed",
    }


def calibration_from_gold_evaluation(evaluation: dict[str, Any] | None) -> dict[str, Any]:
    """Convert the optional gold report into a cautious SYP calibration row."""
    if not isinstance(evaluation, dict):
        return {}
    metrics = evaluation.get("verified_metrics") or {}
    sample_count = int(metrics.get("scorable_count", 0) or 0)
    accuracy = metrics.get("coverage")
    if sample_count <= 0 or accuracy is None:
        return {}
    subtype_counts = evaluation.get("expected_subtype_counts") or {}
    rows: dict[str, dict[str, Any]] = {}
    for subtype, count in subtype_counts.items():
        if str(subtype) == "unknown":
            continue
        rows[str(subtype)] = {
            "accuracy": accuracy,
            "sample_count": int(count or 0),
            "basis": "gold_evaluation.verified_metrics.coverage",
        }
    return {
        "sympointv2": {
            "overall": {
                "accuracy": accuracy,
                "sample_count": sample_count,
                "basis": "gold_evaluation.verified_metrics.coverage",
            },
            "by_subtype": rows,
        }
    }
