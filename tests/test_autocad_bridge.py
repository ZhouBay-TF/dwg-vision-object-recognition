from __future__ import annotations

from dwg_vision.autocad_bridge import extract_com_document


class _Line:
    ObjectName = "AcDbLine"
    Layer = "A-WALL"
    Handle = "10"
    StartPoint = (0.0, 0.0, 0.0)
    EndPoint = (100.0, 0.0, 0.0)


class _Text:
    ObjectName = "AcDbText"
    Layer = "ANNO"
    Handle = "11"
    TextString = "窗"
    InsertionPoint = (20.0, 20.0, 0.0)
    Height = 5.0


class _Circle:
    ObjectName = "AcDbCircle"
    Layer = "A-FURNITURE"
    Handle = "12"
    Center = (50.0, 50.0, 0.0)
    Radius = 2.0

    def GetBoundingBox(self):
        return (48.0, 48.0, 0.0), (52.0, 52.0, 0.0)


class _Frame:
    ObjectName = "AcDbPolyline"
    Layer = "FRAME"
    Handle = "13"
    Coordinates = [0.0, 0.0, 100.0, 0.0, 100.0, 100.0, 0.0, 100.0]
    Closed = True

    def GetBoundingBox(self):
        return (0.0, 0.0, 0.0), (100.0, 100.0, 0.0)


class _PaperLine(_Line):
    Handle = "20"


class _Layout:
    Name = "Layout1"
    Block = [_PaperLine()]


class _Document:
    ModelSpace = [_Line(), _Text(), _Circle(), _Frame()]

    def GetVariable(self, name):
        return (0.0, 0.0, 0.0) if name == "EXTMIN" else (100.0, 100.0, 0.0)


class _DocumentWithLayout(_Document):
    Layouts = [_Layout()]


def test_autocad_like_document_exports_vector_evidence() -> None:
    payload = extract_com_document(_Document())
    assert payload["adapter"] == "autocad_activex"
    assert payload["entities"][0]["type"] == "wall"
    assert payload["annotations"][0]["text"] == "窗"
    assert {item["entity_id"] for item in payload["raw_entities"]} == {"10", "12", "13"}
    assert payload["raw_entities"][1]["command"] == "circle"
    assert payload["frames"][0]["world_bbox"] == [0.0, 0.0, 100.0, 100.0]
    assert payload["annotations"][0]["text_id"] == "11"


def test_autocad_like_document_includes_paper_layout_geometry() -> None:
    payload = extract_com_document(_DocumentWithLayout())
    paper = [item for item in payload["raw_entities"] if item["handle"] == "20"]
    assert len(paper) == 1
    assert paper[0]["provenance"]["space"] == "PaperSpace:Layout1"
