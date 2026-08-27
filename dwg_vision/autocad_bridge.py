from __future__ import annotations

"""Optional AutoCAD 2020-2027 ActiveX bridge.

The bridge is late-bound on purpose.  It can be imported and unit-tested on a
machine without AutoCAD, while a Windows workstation with a supported AutoCAD
release and pywin32 can export vector evidence from the active drawing.
"""

import json
import math
import os
import time
from pathlib import Path
from typing import Any

from .geometry import DOOR_TERMS, FURNITURE_TERMS, WALL_TERMS, WINDOW_TERMS, _terms_type
from .schema import normalize_entity


COM_BUSY_HRESULTS = {
    -2147418111,  # RPC_E_CALL_REJECTED / 0x80010001
    -2147417846,  # RPC_E_SERVERCALL_RETRYLATER / 0x8001010A
    -2147417845,  # RPC_E_SERVERCALL_REJECTED / 0x8001010B
}

# AutoCAD ActiveX keeps the major product number in the ProgID.  The explicit
# entries are ordered newest-first so a workstation with more than one release
# installed uses the newest supported release by default.  The short aliases
# are retained because some installations register ``.25``/``.26`` instead of
# the more explicit ``.25.1``/``.26.0`` form.
AUTOCAD_PROG_IDS: tuple[str, ...] = (
    "AutoCAD.Application.26.0",  # 2027
    "AutoCAD.Application.26",
    "AutoCAD.Application.25.1",  # 2026
    "AutoCAD.Application.25.0",  # 2025
    "AutoCAD.Application.25",
    "AutoCAD.Application.24.3",  # 2024
    "AutoCAD.Application.24.2",  # 2023
    "AutoCAD.Application.24.1",  # 2022
    "AutoCAD.Application.24.0",  # 2021
    "AutoCAD.Application.24",
    "AutoCAD.Application.23.1",  # 2020
)


def _com_error_code(exc: BaseException) -> int | None:
    value = getattr(exc, "hresult", None)
    if value is None and getattr(exc, "args", None):
        value = exc.args[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_com_busy(exc: BaseException) -> bool:
    return _com_error_code(exc) in COM_BUSY_HRESULTS


def _pump_com_messages() -> None:
    try:
        import pythoncom

        pythoncom.PumpWaitingMessages()
    except (ImportError, AttributeError):
        pass


def _com_retry_call(operation: Any, *, name: str, timeout_s: float = 180.0) -> Any:
    """Retry AutoCAD RPC_E_CALL_REJECTED while its UI/document is busy."""
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    last_error: BaseException | None = None
    while True:
        try:
            return operation()
        except Exception as exc:  # COM exceptions are runtime-specific
            if not _is_com_busy(exc):
                raise
            last_error = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(f"AutoCAD COM 操作 {name} 在 {timeout_s:g} 秒内一直忙碌，请完成授权/关闭模态对话框后重试") from last_error
            _pump_com_messages()
            time.sleep(0.5)


def _safe_close_document(document: Any, *, timeout_s: float = 20.0) -> None:
    try:
        _com_retry_call(lambda: document.Close(False), name="Document.Close", timeout_s=timeout_s)
    except Exception:
        # Closing is cleanup; never hide the original extraction error.
        pass


def _point(value: Any) -> tuple[float, float]:
    return float(value[0]), float(value[1])


def _bbox(points: list[tuple[float, float]]) -> list[float]:
    if not points:
        return [0.0, 0.0, 0.0, 0.0]
    return [min(point[0] for point in points), min(point[1] for point in points), max(point[0] for point in points), max(point[1] for point in points)]


def _entity_type(layer: str, block_name: str = "") -> str | None:
    return _terms_type(f"{layer} {block_name}")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_com_attr(item: Any, name: str, default: Any = None) -> Any:
    """Read a late-bound COM property without turning one missing property
    into a whole-document extraction failure.

    AutoCAD proxy objects and version-specific ActiveX wrappers do not expose
    exactly the same optional properties.  The bridge intentionally uses
    conservative defaults and records the stable Handle whenever possible.
    """
    try:
        return getattr(item, name)
    except Exception:
        return default


def _bbox_from_item(item: Any, fallback_points: list[tuple[float, float]] | None = None) -> list[float]:
    try:
        lower_left, upper_right = item.GetBoundingBox()
        return _bbox([_point(lower_left), _point(upper_right)])
    except Exception:
        return _bbox(fallback_points or [])


def _com_color(item: Any) -> Any:
    value = getattr(item, "TrueColor", None)
    if value is not None:
        for attribute in ("Red", "Green", "Blue"):
            if not hasattr(value, attribute):
                break
        else:
            return [int(getattr(value, "Red")), int(getattr(value, "Green")), int(getattr(value, "Blue"))]
    return getattr(item, "Color", 256)


def _polyline_points(item: Any) -> list[tuple[float, float]]:
    raw = list(getattr(item, "Coordinates", []) or [])
    return [(float(raw[index]), float(raw[index + 1])) for index in range(0, len(raw) - 1, 2)]


def _is_frame_like(layer: str, object_name: str, block_name: str = "") -> bool:
    value = f"{layer} {object_name} {block_name}".lower()
    return any(term in value for term in ("frame", "border", "titleblock", "title_block", "图框", "图签", "viewport"))


def _raw_geometry(
    item: Any,
    *,
    index: int,
    layer: str,
    object_name: str,
    handle: str,
    block_name: str,
    entity_type: str | None,
    provenance: dict[str, Any],
) -> dict[str, Any] | None:
    """Export every useful geometric object, even when no class is known."""
    points: list[tuple[float, float]] = []
    dimensions: dict[str, Any] = {}
    command = "unknown"
    if "Line" in object_name and hasattr(item, "StartPoint") and hasattr(item, "EndPoint"):
        points = [_point(item.StartPoint), _point(item.EndPoint)]
        command = "line"
        dimensions["length"] = math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])
    elif "Polyline" in object_name and hasattr(item, "Coordinates"):
        points = _polyline_points(item)
        command = "polyline"
        dimensions["vertex_count"] = len(points)
        dimensions["closed"] = bool(getattr(item, "Closed", False))
    elif "Circle" in object_name and hasattr(item, "Center"):
        center = _point(item.Center)
        radius = _safe_float(getattr(item, "Radius", 0.0))
        points = [center]
        command = "circle"
        dimensions["radius"] = radius
    elif "Arc" in object_name and hasattr(item, "Center"):
        center = _point(item.Center)
        radius = _safe_float(getattr(item, "Radius", 0.0))
        points = [center]
        command = "arc"
        dimensions.update({"radius": radius, "start_angle": _safe_float(getattr(item, "StartAngle", 0.0)), "end_angle": _safe_float(getattr(item, "EndAngle", 0.0))})
    elif "Ellipse" in object_name and hasattr(item, "Center"):
        points = [_point(item.Center)]
        command = "ellipse"
        dimensions["major_radius"] = _safe_float(getattr(item, "MajorRadius", 0.0))
        dimensions["minor_radius"] = _safe_float(getattr(item, "MinorRadius", 0.0))
    elif "BlockReference" in object_name or "Insert" in object_name:
        insertion = getattr(item, "InsertionPoint", (0.0, 0.0, 0.0))
        points = [_point(insertion)]
        command = "block"
        dimensions["rotation_deg"] = _safe_float(getattr(item, "Rotation", 0.0)) * 180.0 / math.pi
    elif "Viewport" in object_name:
        command = "viewport"
        dimensions["twist_angle"] = _safe_float(getattr(item, "TwistAngle", 0.0))
    else:
        return None

    bbox_world = _bbox_from_item(item, points)
    if bbox_world == [0.0, 0.0, 0.0, 0.0] and points:
        bbox_world = _bbox(points)
    is_frame = _is_frame_like(layer, object_name, block_name) and (
        command in {"block", "polyline", "viewport"} and (command in {"block", "viewport"} or bool(getattr(item, "Closed", False)))
    )
    return {
        "entity_id": handle or f"entity_{index:06d}",
        "handle": handle,
        "type": entity_type or "unknown",
        "subtype": block_name or object_name,
        "entity_type": object_name,
        "command": command,
        "bbox_world": bbox_world,
        "points_world": [[x, y] for x, y in points],
        "polygon_world": [[x, y] for x, y in points] if len(points) > 1 else [],
        "dimensions": dimensions,
        "layer": layer,
        "color": _com_color(item),
        "lineweight": _safe_float(getattr(item, "Lineweight", 0.1), 0.1),
        "is_frame": is_frame,
        "provenance": provenance | {"entity_id": handle or f"entity_{index:06d}"},
    }


def _annotation_from_item(item: Any, *, index: int, layer: str, object_name: str, handle: str) -> dict[str, Any] | None:
    is_text = any(term in object_name.lower() for term in ("text", "mtext", "attributereference"))
    is_dimension = "dimension" in object_name.lower() or "leader" in object_name.lower()
    if not is_text and not is_dimension:
        return None
    text = getattr(item, "TextString", None) or getattr(item, "Text", None) or getattr(item, "TextOverride", None) or ""
    if not text and is_dimension:
        measurement = getattr(item, "Measurement", None)
        if measurement is not None:
            text = str(measurement)
    text = str(text).strip()
    insertion = getattr(item, "InsertionPoint", None) or getattr(item, "TextPosition", None) or getattr(item, "Location", (0.0, 0.0, 0.0))
    position = _point(insertion)
    height = max(_safe_float(getattr(item, "Height", None) or getattr(item, "TextHeight", 1.0), 1.0), 1.0)
    bbox_world = _bbox_from_item(item, [(position[0], position[1]), (position[0] + height, position[1] + height)])
    return {
        "text_id": handle or f"text_{index:06d}",
        "handle": handle,
        "text": text,
        "bbox_world": bbox_world,
        "position_world": [position[0], position[1]],
        "height": height,
        "rotation_deg": _safe_float(getattr(item, "Rotation", 0.0)) * 180.0 / math.pi,
        "source": "autocad_com",
        "layer": layer,
        "object_name": object_name,
        # Leaders can carry meaningful callout text; only true dimensions are
        # suppressed from the visual Scene SVG.  Both remain native evidence.
        "role": "dimension" if "dimension" in object_name.lower() else ("leader" if "leader" in object_name.lower() else None),
        "style": {"style_name": str(getattr(item, "StyleName", "") or "")},
    }


def _document_spaces(document: Any) -> list[tuple[str, Any]]:
    spaces: list[tuple[str, Any]] = [("ModelSpace", getattr(document, "ModelSpace"))]
    try:
        layouts = getattr(document, "Layouts")
        for layout in layouts:
            name = str(getattr(layout, "Name", ""))
            block = getattr(layout, "Block", None)
            if block is not None and name.lower() != "model":
                spaces.append((f"PaperSpace:{name}", block))
    except Exception:
        # Some COM mocks and older AutoCAD automation contexts do not expose
        # Layouts. ModelSpace remains a valid raw export in that case.
        pass
    return spaces


def extract_com_document(document: Any) -> dict[str, Any]:
    """Extract complete raw geometry plus classified compatibility candidates."""
    entities: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    raw_entities: list[dict[str, Any]] = []
    frame_candidates: list[dict[str, Any]] = []
    seen_handles: set[str] = set()
    entity_index = 0
    for space_name, modelspace in _document_spaces(document):
      for item in modelspace:
        index = entity_index
        entity_index += 1
        layer = str(getattr(item, "Layer", ""))
        object_name = str(getattr(item, "ObjectName", ""))
        handle = str(getattr(item, "Handle", ""))
        if handle and handle in seen_handles:
            continue
        if handle:
            seen_handles.add(handle)
        block_name = str(getattr(item, "Name", "")) if "BlockReference" in object_name else ""
        entity_type = _entity_type(layer, block_name)
        provenance = {"source": "autocad_com", "handle": handle, "layer": layer, "object_name": object_name, "space": space_name}

        annotation = _annotation_from_item(item, index=index, layer=layer, object_name=object_name, handle=handle)
        if annotation is not None:
            annotations.append(annotation)
            continue

        raw_entity = _raw_geometry(
            item,
            index=index,
            layer=layer,
            object_name=object_name,
            handle=handle,
            block_name=block_name,
            entity_type=entity_type,
            provenance=provenance,
        )
        if raw_entity is None:
            continue
        raw_entities.append(raw_entity)
        if raw_entity["is_frame"]:
            frame_candidates.append({
                "scene_id": f"scene_frame_{len(frame_candidates) + 1:04d}",
                "frame_type": "cad_frame_candidate",
                "world_bbox": raw_entity["bbox_world"],
                "layout_id": None,
                "source_entity_id": raw_entity["entity_id"],
            })

        if "Line" in object_name and hasattr(item, "StartPoint") and hasattr(item, "EndPoint"):
            points = [_point(item.StartPoint), _point(item.EndPoint)]
            if entity_type:
                entities.append(
                    normalize_entity(
                        {
                            "type": entity_type,
                            "subtype": "line",
                            "bbox_px": _bbox(points),
                            "polygon_px": [[x, y] for x, y in points],
                            "dimensions": {"length": ((points[1][0] - points[0][0]) ** 2 + (points[1][1] - points[0][1]) ** 2) ** 0.5},
                            "confidence": 0.82,
                            "evidence": {"method": "autocad_com", "layer": layer},
                            "provenance": provenance,
                        },
                        default_source="autocad",
                    )
                )
        elif "Polyline" in object_name and hasattr(item, "Coordinates"):
            raw = list(item.Coordinates)
            points = [(float(raw[index]), float(raw[index + 1])) for index in range(0, len(raw) - 1, 2)]
            if entity_type and points:
                entities.append(
                    normalize_entity(
                        {
                            "type": entity_type,
                            "subtype": "polyline",
                            "bbox_px": _bbox(points),
                            "polygon_px": [[x, y] for x, y in points],
                            "dimensions": {"vertex_count": len(points)},
                            "confidence": 0.82,
                            "evidence": {"method": "autocad_com", "layer": layer},
                            "provenance": provenance,
                        },
                        default_source="autocad",
                    )
                )
        elif "BlockReference" in object_name or "Insert" in object_name:
            points: list[tuple[float, float]] = []
            try:
                lower_left, upper_right = item.GetBoundingBox()
                points = [_point(lower_left), _point(upper_right)]
            except Exception:
                insertion = getattr(item, "InsertionPoint", (0.0, 0.0, 0.0))
                points = [_point(insertion), (float(insertion[0]) + 1.0, float(insertion[1]) + 1.0)]
            if entity_type:
                entities.append(
                    normalize_entity(
                        {
                            "type": entity_type,
                            "subtype": block_name or "block",
                            "bbox_px": _bbox(points),
                            "rotation_deg": float(getattr(item, "Rotation", 0.0) or 0.0) * 180.0 / 3.141592653589793,
                            "dimensions": {},
                            "confidence": 0.86,
                            "evidence": {"method": "autocad_com", "layer": layer, "block_name": block_name},
                            "provenance": provenance | {"block_name": block_name},
                        },
                        default_source="autocad",
                    )
                )

    bounds = None
    try:
        extmin = document.GetVariable("EXTMIN")
        extmax = document.GetVariable("EXTMAX")
        bounds = [float(extmin[0]), float(extmin[1]), float(extmax[0]), float(extmax[1])]
    except Exception:
        pass
    return {
        "schema_version": "dwg_raw.v1",
        "coordinate_space": "world",
        "coordinate_system": {"space": "world", "units": "drawing_units", "world_bounds": bounds},
        "entities": entities,
        "raw_entities": raw_entities,
        "annotations": annotations,
        "frames": frame_candidates,
        "adapter": "autocad_activex",
    }


def export_with_autocad(
    dwg_path: Path,
    output_json: Path,
    *,
    prog_id: str | None = None,
    visible: bool = False,
) -> Path:
    """Open a DWG in AutoCAD 2020-2027 through ActiveX and export evidence."""
    try:
        import win32com.client
    except ImportError as exc:  # pragma: no cover - Windows optional path
        raise RuntimeError("AutoCAD 2020-2027 ActiveX 适配需要 pywin32：python -m pip install pywin32") from exc

    configured_prog_id = prog_id or os.getenv("AUTOCAD_PROG_ID")
    candidates = [configured_prog_id] if configured_prog_id else list(AUTOCAD_PROG_IDS)
    application = None
    last_error: Exception | None = None
    selected_prog_id = candidates[0]
    for candidate in candidates:
        try:
            application = win32com.client.Dispatch(candidate)
            selected_prog_id = candidate
            break
        except Exception as exc:  # pragma: no cover - depends on local AutoCAD
            last_error = exc
    if application is None:
        raise RuntimeError(f"无法连接 AutoCAD（尝试 ProgID={', '.join(candidates)}），请确认 AutoCAD 已安装并已完成首次启动") from last_error
    timeout_s = float(os.getenv("AUTOCAD_COM_TIMEOUT_S", "180") or 180.0)
    _com_retry_call(lambda: setattr(application, "Visible", bool(visible)), name="Application.Visible", timeout_s=timeout_s)
    document = _com_retry_call(
        lambda: application.Documents.Open(str(dwg_path.resolve())),
        name="Documents.Open",
        timeout_s=timeout_s,
    )
    try:
        try:
            _com_retry_call(lambda: document.Activate(), name="Document.Activate", timeout_s=timeout_s)
        except AttributeError:
            pass
        payload = _com_retry_call(lambda: extract_com_document(document), name="extract_com_document", timeout_s=timeout_s)
        application_version = str(_safe_com_attr(application, "Version", "") or "")
        application_name = str(_safe_com_attr(application, "Name", "AutoCAD") or "AutoCAD")
        payload["adapter"] = "autocad_activex"
        payload["cad_provenance"] = {
            "software": application_name,
            "version": application_version,
            "prog_id": selected_prog_id,
            "api": "ActiveX/COM",
            "supported_major_releases": [2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027],
        }
    finally:
        _safe_close_document(document)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_json


# Backward-compatible import name used by the first development iteration.
export_with_autocad_2026 = export_with_autocad
