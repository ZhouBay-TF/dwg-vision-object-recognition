from __future__ import annotations

"""Bounded visual-review state machine.

This is intentionally not a general CAD agent.  It can only choose from the
fixed view profiles below, call a supplied vision provider, and return an
auditable observation trace for an already known candidate object.
"""

import json
from pathlib import Path
from typing import Any, Iterable

from .providers import VisionProvider


VIEW_PROFILES: tuple[str, ...] = ("tight", "context", "wall_context", "text_context")
VIEW_MARGIN_FACTORS = {
    "tight": 0.15,
    "context": 0.60,
    "wall_context": 1.20,
    "text_context": 1.20,
}


def _scene_overview_prompt(scene: dict[str, Any], texts: list[dict[str, Any]]) -> str:
    local_bounds = scene.get("local_bounds") or [0, 0, 0, 0]
    # A dense CAD tile can contain hundreds of dimension/annotation strings.
    # Keep the overview request bounded for OpenAI-compatible gateways; the
    # full native text evidence remains available to downstream rules.
    prompt_texts = texts[:40]
    text_lines = "\n".join(
        f"- {item.get('text_id', '')}: {str(item.get('text', ''))[:80]} [{item.get('role', 'general_annotation')}]"
        for item in prompt_texts
    ) or "（当前 Scene 没有可读原生文字）"
    omitted = max(0, len(texts) - len(prompt_texts))
    if omitted:
        text_lines += f"\n- 另有 {omitted} 条文字未展开，请以图像为准。"
    return f"""你是建筑 CAD 图框级视觉理解器。当前图片已经是一个独立图框 Scene 的完整高清图，不是整张 DWG 拼图。

Scene ID：{scene.get('scene_id', '')}
Scene 局部范围：x=0..{local_bounds[2]}，y=0..{local_bounds[3]}
原生文字证据（文字优先，但必须以图中可见内容核验）：
{text_lines}

请先建立这个图框的全局视觉先验，供后续 CAD 几何模型和局部复核使用：
1. 概括空间布局、主要房间/区域、墙体组织和图纸可读性。
2. 列出图中明确可见的对象类别与区域，但不要把它们直接当作最终检测结果。
3. 指出可能需要后续局部复核的区域、符号与文字不一致、遮挡或低清晰度问题。
4. 文字只能作为证据，不得修改坐标、Handle 或几何对象。
5. 不要输出思维链，只输出简短、可审计的观察事实。

严格返回 JSON：
{{
  "scene_summary": "",
  "rooms_or_zones": [{{"name": "", "evidence": ""}}],
  "global_object_hypotheses": [{{"type": "wall|window|door|furniture", "subtype": "", "region": "", "evidence": "", "confidence": 0.0}}],
  "text_observations": [{{"text": "", "meaning": "", "confidence": 0.0}}],
  "review_regions": [{{"region": "", "reason": ""}}],
  "global_warnings": [],
  "notes": ""
}}"""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _normalize_scene_overview(
    response: dict[str, Any],
    *,
    scene_id: str,
    image_path: Path | None,
    model: str,
) -> dict[str, Any]:
    hypotheses = response.get("global_object_hypotheses") or response.get("objects") or []
    if not isinstance(hypotheses, list):
        hypotheses = []
    rooms = response.get("rooms_or_zones") or response.get("rooms") or []
    if not isinstance(rooms, list):
        rooms = []
    text_observations = response.get("text_observations") or []
    if not isinstance(text_observations, list):
        text_observations = []
    review_regions = response.get("review_regions") or []
    if not isinstance(review_regions, list):
        review_regions = []
    return {
        "schema_version": "visual_scene_overview.v1",
        "scene_id": scene_id,
        "view_profile": "scene_overview",
        "image_path": str(image_path) if image_path else None,
        "model": model,
        "status": "completed",
        "scene_summary": str(response.get("scene_summary") or response.get("summary") or response.get("notes") or ""),
        "rooms_or_zones": rooms,
        "global_object_hypotheses": hypotheses,
        "text_observations": text_observations,
        "review_regions": review_regions,
        "global_warnings": _string_list(response.get("global_warnings") or response.get("warnings")),
        "notes": str(response.get("notes") or ""),
    }


def _clamp_box(box: list[float], width: int, height: int) -> list[int]:
    left, top, right, bottom = [float(value) for value in box]
    left, right = max(0.0, min(left, width)), max(0.0, min(right, width))
    top, bottom = max(0.0, min(top, height)), max(0.0, min(bottom, height))
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    return [round(left), round(top), round(right), round(bottom)]


def _world_to_pixel(box: list[float], manifest: dict[str, Any]) -> list[float]:
    if manifest.get("world_to_pixel"):
        transform = manifest["world_to_pixel"]
        origin = transform.get("origin_world") or [0.0, 0.0]
        scale_x = float(transform.get("scale_x", 1.0) or 1.0)
        scale_y = float(transform.get("scale_y", 1.0) or 1.0)
        return [
            (box[0] - origin[0]) * scale_x,
            (box[1] - origin[1]) * scale_y,
            (box[2] - origin[0]) * scale_x,
            (box[3] - origin[1]) * scale_y,
        ]
    bounds = manifest.get("world_bounds")
    width = float(manifest.get("width_px") or manifest.get("width") or 1.0)
    height = float(manifest.get("height_px") or manifest.get("height") or 1.0)
    if bounds and len(bounds) == 4:
        span_x = max(1e-9, float(bounds[2]) - float(bounds[0]))
        span_y = max(1e-9, float(bounds[3]) - float(bounds[1]))
        return [
            (box[0] - bounds[0]) / span_x * width,
            (box[1] - bounds[1]) / span_y * height,
            (box[2] - bounds[0]) / span_x * width,
            (box[3] - bounds[1]) / span_y * height,
        ]
    return list(box)


def _candidate_pixel_bbox(candidate: dict[str, Any], render_manifest: dict[str, Any]) -> list[float]:
    geometry = candidate.get("geometry_world") or {}
    if geometry.get("bbox"):
        return _world_to_pixel([float(value) for value in geometry["bbox"]], render_manifest)
    return [float(value) for value in candidate.get("bbox_px") or [0, 0, 1, 1]]


def _roi_box(candidate: dict[str, Any], profile: str, render_manifest: dict[str, Any]) -> list[float]:
    base = _candidate_pixel_bbox(candidate, render_manifest)
    width = max(2.0, base[2] - base[0])
    height = max(2.0, base[3] - base[1])
    margin = max(width, height) * VIEW_MARGIN_FACTORS[profile]
    return [base[0] - margin, base[1] - margin, base[2] + margin, base[3] + margin]


def _nearby_text(candidate: dict[str, Any], texts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = candidate.get("evidence") or {}
    values = evidence.get("nearby_text") or []
    if values:
        return [{"text": str(value)} for value in values]
    return []


def _prompt(
    candidate: dict[str, Any],
    profile: str,
    nearby_text: list[dict[str, Any]],
    scene_overview: dict[str, Any] | None = None,
) -> str:
    overview_hint = {
        "scene_summary": (scene_overview or {}).get("scene_summary", ""),
        "rooms_or_zones": (scene_overview or {}).get("rooms_or_zones", [])[:20],
        "global_object_hypotheses": (scene_overview or {}).get("global_object_hypotheses", [])[:20],
        "text_observations": (scene_overview or {}).get("text_observations", [])[:20],
        "review_regions": (scene_overview or {}).get("review_regions", [])[:20],
        "global_warnings": (scene_overview or {}).get("global_warnings", []),
    }
    return f"""你是建筑 CAD 局部视觉复核器。当前对象来自稳定候选结果，不能凭空新增对象。

对象类型：{candidate.get('type', 'unknown')}
当前子类：{candidate.get('subtype', 'unknown')}
视图模板：{profile}
附近原生文字：{json.dumps(nearby_text, ensure_ascii=False)}
Scene 全图视觉概览（仅作待核验先验，不是最终结论）：{json.dumps(overview_hint, ensure_ascii=False)}

请只判断当前对象是否支持更细的类别，并返回 JSON：
{{
  "category": "简短中文或英文类别",
  "confidence": 0.0,
  "visible_facts": ["图中可复核的事实"],
  "uncertainty": ["无法确认的内容"],
  "contradictions": ["与文字或候选冲突的内容"]
}}

不要输出思维链，不要修改对象坐标，不要创建其他对象。"""


def _normalize_observation(
    response: dict[str, Any],
    *,
    object_id: str,
    instance_id: str,
    scene_id: str,
    profile: str,
    image_path: Path | None,
    model: str,
    attempt: int,
) -> dict[str, Any]:
    detections = response.get("detections") or []
    first = detections[0] if isinstance(detections, list) and detections and isinstance(detections[0], dict) else {}
    category = str(response.get("category") or first.get("subtype") or first.get("category") or first.get("type") or "")
    visible_facts = response.get("visible_facts") or first.get("visible_facts") or []
    if isinstance(visible_facts, str):
        visible_facts = [visible_facts]
    uncertainty = response.get("uncertainty") or first.get("uncertainty") or []
    if isinstance(uncertainty, str):
        uncertainty = [uncertainty]
    contradictions = response.get("contradictions") or first.get("contradictions") or []
    if isinstance(contradictions, str):
        contradictions = [contradictions]
    evidence = response.get("evidence") or first.get("evidence") or response.get("notes") or ""
    return {
        "schema_version": "visual_observation.v1",
        "observation_id": f"{object_id}_obs_{attempt:02d}",
        "scene_id": scene_id,
        "object_id": object_id,
        "instance_id": instance_id,
        "view_profile": profile,
        "image_path": str(image_path) if image_path else None,
        "model": model,
        "category": category,
        "confidence": max(0.0, min(1.0, float(response.get("confidence", first.get("confidence", 0.0)) or 0.0))),
        "visible_facts": [str(item) for item in visible_facts],
        "uncertainty": [str(item) for item in uncertainty],
        "contradictions": [str(item) for item in contradictions],
        "evidence": str(evidence),
    }


class VisualReviewAgent:
    """A bounded state machine for multi-view candidate review."""

    def __init__(
        self,
        provider: VisionProvider | None = None,
        *,
        max_attempts_per_object: int = 3,
        max_attempts_per_scene: int = 30,
        view_profiles: tuple[str, ...] = VIEW_PROFILES,
    ) -> None:
        invalid = [profile for profile in view_profiles if profile not in VIEW_MARGIN_FACTORS]
        if invalid:
            raise ValueError(f"unsupported visual review profiles: {invalid}")
        self.provider = provider
        self.max_attempts_per_object = max(1, int(max_attempts_per_object))
        self.max_attempts_per_scene = max(1, int(max_attempts_per_scene))
        self.view_profiles = view_profiles

    def overview(
        self,
        scene: dict[str, Any],
        *,
        render_path: Path | None = None,
        output_dir: Path | None = None,
        texts: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Call the vision model once on the complete Scene-local image.

        This is a global prior only. It cannot create final objects or mutate
        CAD geometry; later SymPoint/text/ROI evidence remains authoritative.
        """
        scene_id = str(scene["scene_id"])
        image_path = Path(render_path) if render_path else None
        action: dict[str, Any] = {
            "action": "call_scene_overview",
            "scene_id": scene_id,
            "image_path": str(image_path) if image_path else None,
            "status": "skipped_no_provider_or_render",
        }
        trace: dict[str, Any] = {
            "schema_version": "visual_scene_overview.v1",
            "scene_id": scene_id,
            "budget": {"max_attempts": 1, "used_attempts": 0},
            "actions": [action],
            "status": "skipped_no_provider_or_render",
        }
        if self.provider is None or image_path is None or not image_path.is_file():
            return trace
        try:
            response = self.provider.analyze(
                image_path,
                _scene_overview_prompt(scene, list(texts)),
            )
            overview = _normalize_scene_overview(
                response,
                scene_id=scene_id,
                image_path=image_path,
                model=str(getattr(self.provider, "model", "unknown")),
            )
            action.update({"status": "completed"})
            trace.update({
                "status": "completed",
                "budget": {"max_attempts": 1, "used_attempts": 1},
                "overview": overview,
            })
        except Exception as exc:  # noqa: BLE001
            action.update({"status": "failed", "error": str(exc)})
            trace.update({"status": "failed", "error": str(exc)})
        return trace

    def _render_roi(
        self,
        candidate: dict[str, Any],
        profile: str,
        render_path: Path | None,
        render_manifest: dict[str, Any],
        output_dir: Path,
    ) -> Path | None:
        if render_path is None or not Path(render_path).is_file():
            return None
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("视觉 ROI 生成需要 Pillow") from exc
        image_path = Path(render_path)
        with Image.open(image_path) as image:
            box = _clamp_box(_roi_box(candidate, profile, render_manifest), image.width, image.height)
            if box[2] <= box[0] or box[3] <= box[1]:
                return None
            crop = image.crop(tuple(box))
            output_path = output_dir / f"{candidate['object_id']}_{profile}.png"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            crop.save(output_path)
            return output_path

    def review(
        self,
        scene: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        render_path: Path | None = None,
        render_manifest: dict[str, Any] | None = None,
        output_dir: Path | None = None,
        texts: Iterable[dict[str, Any]] = (),
        scene_overview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest = dict(render_manifest or {})
        root = Path(output_dir or "visual_review") / str(scene["scene_id"])
        observations: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        calls = 0
        for candidate in candidates:
            if calls >= self.max_attempts_per_scene:
                break
            object_id = str(candidate.get("object_id") or "")
            instance_id = str(candidate.get("instance_id") or candidate.get("object_id") or object_id)
            if not object_id:
                continue
            task_attempts: list[dict[str, Any]] = []
            categories: list[str] = []
            nearby_text = _nearby_text(candidate, texts)
            for attempt, profile in enumerate(self.view_profiles[: self.max_attempts_per_object], start=1):
                if calls >= self.max_attempts_per_scene:
                    break
                roi_path = self._render_roi(candidate, profile, render_path, manifest, root)
                action = {
                    "action": "call_vision",
                    "object_id": object_id,
                    "view_profile": profile,
                    "remaining_attempts": self.max_attempts_per_object - attempt,
                    "image_path": str(roi_path) if roi_path else None,
                }
                calls += 1
                if self.provider is None or roi_path is None:
                    action["status"] = "skipped_no_provider_or_render"
                    task_attempts.append(action)
                    continue
                try:
                    response = self.provider.analyze(
                        roi_path,
                        _prompt(candidate, profile, nearby_text, scene_overview),
                    )
                    observation = _normalize_observation(
                        response,
                        object_id=object_id,
                        instance_id=instance_id,
                        scene_id=str(scene["scene_id"]),
                        profile=profile,
                        image_path=roi_path,
                        model=str(getattr(self.provider, "model", "unknown")),
                        attempt=attempt,
                    )
                    observations.append(observation)
                    category = observation.get("category", "")
                    if category:
                        categories.append(category)
                    action.update({"status": "completed", "observation_id": observation["observation_id"]})
                    task_attempts.append(action)
                    if len(categories) >= 2 and categories[-1].lower() == categories[-2].lower():
                        break
                except Exception as exc:  # noqa: BLE001
                    action.update({"status": "failed", "error": str(exc)})
                    task_attempts.append(action)
            tasks.append({
                "object_id": object_id,
                "status": "needs_fusion" if task_attempts else "skipped",
                "attempts": task_attempts,
            })
        return {
            "schema_version": "visual_review_trace.v1",
            "scene_id": scene["scene_id"],
            "budget": {
                "max_attempts_per_object": self.max_attempts_per_object,
                "max_attempts_per_scene": self.max_attempts_per_scene,
                "used_attempts": calls,
            },
            "tasks": tasks,
            "observations": observations,
            "scene_overview_used": scene_overview is not None,
        }
