from __future__ import annotations

from PIL import Image

from dwg_vision.providers import CallableVisionProvider
from dwg_vision.visual_review_agent import VisualReviewAgent


def test_visual_review_agent_is_bounded_and_generates_roi(tmp_path) -> None:
    render = tmp_path / "render.png"
    Image.new("RGB", (400, 300), "white").save(render)
    seen: list[str] = []

    def callback(path, prompt):
        seen.append(path.name)
        return {
            "category": "wall_hung_toilet",
            "confidence": 0.8,
            "visible_facts": ["对象靠墙"],
            "uncertainty": [],
            "contradictions": [],
        }

    provider = CallableVisionProvider("test", "test-model", callback)
    agent = VisualReviewAgent(provider, max_attempts_per_object=3, max_attempts_per_scene=3)
    trace = agent.review(
        {"scene_id": "scene_1"},
        [{
            "object_id": "obj_1", "type": "furniture", "subtype": "toilet",
            "geometry_world": {"bbox": [100, 100, 160, 160]},
        }],
        render_path=render,
        render_manifest={"world_bounds": [0, 0, 400, 300], "width_px": 400, "height_px": 300},
        output_dir=tmp_path / "review",
    )
    assert trace["budget"]["used_attempts"] == 2
    assert len(trace["observations"]) == 2
    assert trace["observations"][0]["category"] == "wall_hung_toilet"
    assert seen
    assert (tmp_path / "review" / "scene_1" / "obj_1_tight.png").is_file()


def test_scene_overview_is_one_full_scene_call(tmp_path) -> None:
    render = tmp_path / "scene.png"
    Image.new("RGB", (400, 300), "white").save(render)
    calls: list[str] = []

    def callback(path, prompt):
        calls.append(prompt)
        return {
            "scene_summary": "一个包含客厅和卫生间的图框",
            "rooms_or_zones": [{"name": "卫生间", "evidence": "原生文字与空间布局"}],
            "global_object_hypotheses": [{"type": "furniture", "subtype": "toilet", "region": "右下区域", "evidence": "可见洁具轮廓", "confidence": 0.8}],
            "text_observations": [],
            "review_regions": [{"region": "右下区域", "reason": "符号与文字需要复核"}],
            "global_warnings": [],
            "notes": "overview only",
        }

    agent = VisualReviewAgent(CallableVisionProvider("test", "test-model", callback))
    trace = agent.overview(
        {"scene_id": "scene_1", "local_bounds": [0, 0, 400, 300]},
        render_path=render,
        texts=[{"text_id": "txt_1", "text": "卫生间", "role": "room_label"}],
    )
    assert trace["status"] == "completed"
    assert trace["budget"]["used_attempts"] == 1
    assert trace["overview"]["scene_summary"].startswith("一个包含")
    assert len(calls) == 1
