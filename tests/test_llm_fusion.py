from __future__ import annotations

from dwg_vision.llm_fusion import DeepSeekFusionProvider, apply_fusion_decisions


def test_deepseek_fusion_enables_reasoning_without_persisting_chain(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_FUSION_MODEL", "deepseek-v4-pro")
    provider = DeepSeekFusionProvider()
    calls = {}

    class Message:
        content = '{"decisions": [], "notes": "structured result"}'

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    def fake_create(**kwargs):
        calls.update(kwargs)
        return Response()

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)
    result = provider.fuse({"objects": []})

    assert result["notes"] == "structured result"
    assert calls["model"] == "deepseek-v4-pro"
    assert calls["reasoning_effort"] == "high"
    assert calls["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_content" not in result


def test_llm_fusion_cannot_mutate_geometry_or_override_text() -> None:
    graph = {
        "objects": [
            {
                "object_id": "obj_text",
                "subtype": "toilet",
                "semantic_source": "native_text",
                "confidence": 0.9,
                "geometry_world": {"bbox": [1, 2, 3, 4]},
                "source_handles": ["A1"],
            },
            {
                "object_id": "obj_model",
                "subtype": "furniture",
                "semantic_source": "sympointv2",
                "confidence": 0.5,
                "geometry_world": {"bbox": [5, 6, 7, 8]},
                "source_handles": ["A2"],
            },
        ]
    }
    result = apply_fusion_decisions(
        graph,
        {
            "decisions": [
                {"object_id": "obj_text", "subtype": "sink", "confidence": 0.99, "conflict_codes": [], "evidence_refs": []},
                {"object_id": "obj_model", "subtype": "sofa", "confidence": 0.8, "conflict_codes": [], "evidence_refs": [], "bbox_world": [0, 0, 99, 99]},
            ],
            "notes": "test",
        },
        provider="test",
        model="test-model",
    )
    assert result["objects"][0]["subtype"] == "toilet"
    assert result["objects"][1]["subtype"] == "furniture"
    assert result["objects"][1]["geometry_world"]["bbox"] == [5, 6, 7, 8]
    assert len(result["fusion"]["rejected"]) == 2
