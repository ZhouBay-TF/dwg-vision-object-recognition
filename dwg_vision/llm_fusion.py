from __future__ import annotations

"""Optional LLM adapter used *inside* Scene Graph fusion.

The adapter is deliberately narrower than an Agent: it receives structured
evidence and may only suggest semantic fields for existing object IDs.
"""

import json
import os
import re
from typing import Any, Protocol


FUSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "object_id": {"type": "string"},
                    "subtype": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "conflict_codes": {"type": "array", "items": {"type": "string"}},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["object_id", "subtype", "confidence", "conflict_codes", "evidence_refs"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["decisions", "notes"],
}


class FusionProvider(Protocol):
    name: str
    model: str

    def fuse(self, evidence: dict[str, Any]) -> dict[str, Any]:
        ...


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM fusion response must be a JSON object")
    parsed.setdefault("decisions", [])
    parsed.setdefault("notes", "")
    return parsed


class DeepSeekFusionProvider:
    name = "deepseek"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        selected_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not selected_key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置，无法启用 LLM Scene Graph 融合")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("LLM 融合需要安装 openai") from exc
        self.model = model or os.getenv("DEEPSEEK_FUSION_MODEL", "deepseek-v4-pro")
        self._client = OpenAI(
            api_key=selected_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )

    def fuse(self, evidence: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""你是建筑 CAD Scene Graph 语义融合器。只处理已有 object_id，不得创建新对象。

输入证据：
{json.dumps(evidence, ensure_ascii=False, indent=2)}

规则：
1. CAD 几何、world bbox、Handle、primitive_id 已经确定，不能修改。
2. 直接指向设备的原生 TEXT/MTEXT/属性文字语义权重最高。
3. 文字与 SymPointV2/视觉不一致时保留冲突码，不要伪造确定性。
4. 只输出 object_id、subtype、confidence、conflict_codes、evidence_refs。
5. 不输出思维链，只输出简短 notes 和 JSON。

严格返回：{{"decisions": [...], "notes": "..."}}"""
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "你是严谨的 CAD 结构化证据融合器。"},
                {"role": "user", "content": prompt},
            ],
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("LLM fusion returned no choices")
        content = getattr(getattr(choices[0], "message", None), "content", "")
        return _parse_json(str(content))


def apply_fusion_decisions(graph: dict[str, Any], response: dict[str, Any], *, provider: str, model: str) -> dict[str, Any]:
    """Apply only safe semantic fields from an LLM response."""
    object_map = {str(item.get("object_id")): item for item in graph.get("objects", [])}
    applied: list[str] = []
    rejected: list[dict[str, Any]] = []
    for decision in response.get("decisions") or []:
        if not isinstance(decision, dict):
            rejected.append({"reason": "decision_not_object"})
            continue
        object_id = str(decision.get("object_id") or "")
        target = object_map.get(object_id)
        if target is None:
            rejected.append({"object_id": object_id, "reason": "unknown_object_id"})
            continue
        # Native equipment text has already won the semantic priority rule.
        if target.get("semantic_source") == "native_text":
            target.setdefault("evidence", {}).setdefault("llm_rejected", []).append({
                "provider": provider,
                "model": model,
                "decision": decision,
                "reason": "native_text_priority",
            })
            rejected.append({"object_id": object_id, "reason": "native_text_priority"})
            continue
        subtype = str(decision.get("subtype") or "").strip()
        if not subtype or any(key in decision for key in ("bbox", "bbox_world", "polygon", "source_handles", "primitive_ids")):
            rejected.append({"object_id": object_id, "reason": "invalid_or_geometry_mutation"})
            continue
        target["subtype"] = subtype
        target["semantic_source"] = "llm_fusion"
        target["confidence"] = max(0.0, min(1.0, float(decision.get("confidence", target.get("confidence", 0.0)) or 0.0)))
        target.setdefault("evidence", {}).setdefault("llm", []).append({
            "provider": provider,
            "model": model,
            "conflict_codes": list(decision.get("conflict_codes") or []),
            "evidence_refs": list(decision.get("evidence_refs") or []),
        })
        if decision.get("conflict_codes"):
            target["status"] = "review"
            validation = graph.setdefault("validation", {"status": "ok", "issues": [], "summary": {"error": 0, "warning": 0, "info": 0}})
            validation["status"] = "warning" if validation.get("status") == "ok" else validation.get("status", "warning")
            validation.setdefault("issues", []).append({
                "severity": "warning",
                "code": "llm_fusion_conflict",
                "message": "LLM 融合保留了语义冲突，需要人工复核",
                "entity_id": object_id,
                "details": list(decision.get("conflict_codes") or []),
            })
        applied.append(object_id)
    validation = graph.get("validation")
    if isinstance(validation, dict):
        summary = {"error": 0, "warning": 0, "info": 0}
        for issue in validation.get("issues", []):
            severity = str(issue.get("severity") or "warning")
            summary[severity] = summary.get(severity, 0) + 1
        validation["summary"] = summary
    graph.setdefault("fusion", {})
    graph["fusion"].update({
        "provider": provider,
        "model": model,
        "notes": str(response.get("notes") or ""),
        "applied_object_ids": applied,
        "rejected": rejected,
    })
    return graph
