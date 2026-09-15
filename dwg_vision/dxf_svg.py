"""Render visible DXF geometry, including nested blocks, to portable SVG."""
from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
from xml.etree import ElementTree as ET


def _fit_circle(first: complex, middle: complex, last: complex) -> tuple[complex, float] | None:
    """Fit a circle through three SVG points."""
    x1, y1 = first.real, first.imag
    x2, y2 = middle.real, middle.imag
    x3, y3 = last.real, last.imag
    denominator = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(denominator) <= 1e-9:
        return None
    center_x = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / denominator
    center_y = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / denominator
    center = complex(center_x, center_y)
    radius = abs(first - center)
    return center, radius if radius > 1e-9 else None


def _circular_curve(segment) -> tuple[complex, float, float] | None:
    """Return (center, radius, relative residual) for a circular Bézier."""
    if segment.__class__.__name__ not in {"CubicBezier", "QuadraticBezier"}:
        return None
    samples = [complex(segment.point(index / 16.0)) for index in range(17)]
    fitted = _fit_circle(samples[0], samples[8], samples[-1])
    if fitted is None:
        return None
    center, radius = fitted
    residual = max(abs(abs(point - center) - radius) for point in samples) / radius
    return center, radius, residual


def _arc_midpoint(
    first: complex,
    last: complex,
    radius: float,
    large_arc: int,
    sweep: int,
) -> complex | None:
    """Return the midpoint of one SVG circular-arc candidate."""
    dx = (first.real - last.real) / 2.0
    dy = (first.imag - last.imag) / 2.0
    distance_sq = dx * dx + dy * dy
    radius_sq = radius * radius
    if distance_sq <= 1e-12 or distance_sq > radius_sq + 1e-6:
        return None
    coefficient = ((radius_sq - distance_sq) / distance_sq) ** 0.5
    if int(large_arc) == int(sweep):
        coefficient = -coefficient
    center_x = (first.real + last.real) / 2.0 + coefficient * dy
    center_y = (first.imag + last.imag) / 2.0 - coefficient * dx
    start_angle = math.atan2(first.imag - center_y, first.real - center_x)
    end_angle = math.atan2(last.imag - center_y, last.real - center_x)
    delta = end_angle - start_angle
    if sweep and delta < 0.0:
        delta += 2.0 * math.pi
    elif not sweep and delta > 0.0:
        delta -= 2.0 * math.pi
    mid_angle = start_angle + delta / 2.0
    return complex(center_x + radius * math.cos(mid_angle), center_y + radius * math.sin(mid_angle))


def _arc_flags(first: complex, middle: complex, last: complex, radius: float) -> tuple[int, int]:
    """Choose SVG arc flags whose midpoint follows the native Bézier."""
    candidates: list[tuple[float, int, int]] = []
    for large_arc in (0, 1):
        for sweep in (0, 1):
            candidate = _arc_midpoint(first, last, radius, large_arc, sweep)
            if candidate is not None:
                candidates.append((abs(candidate - middle), large_arc, sweep))
    if not candidates:
        return 0, 0
    _distance, large_arc, sweep = min(candidates)
    return large_arc, sweep


def _full_ellipse(parsed) -> tuple[float, float, float, float, float] | None:
    """Detect an axis-aligned circle/ellipse emitted as four Bézier arcs."""
    if len(parsed) != 4 or not all(
        segment.__class__.__name__ in {"CubicBezier", "QuadraticBezier"} for segment in parsed
    ):
        return None
    points = [complex(segment.point(index / 16.0)) for segment in parsed for index in range(17)]
    x_values = [point.real for point in points]
    y_values = [point.imag for point in points]
    center_x = (min(x_values) + max(x_values)) / 2.0
    center_y = (min(y_values) + max(y_values)) / 2.0
    radius_x = (max(x_values) - min(x_values)) / 2.0
    radius_y = (max(y_values) - min(y_values)) / 2.0
    if radius_x <= 1e-9 or radius_y <= 1e-9:
        return None
    residual = max(
        abs(((point.real - center_x) / radius_x) ** 2 + ((point.imag - center_y) / radius_y) ** 2 - 1.0)
        for point in points
    )
    if residual > 0.003:
        return None
    return center_x, center_y, radius_x, radius_y, residual


def inline_svg_styles(root: ET.Element) -> None:
    """MuPDF ignores CSS classes in SVG; give every mark its resolved style."""
    import re

    styles: dict[str, dict[str, str]] = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "style":
            continue
        for name, body in re.findall(r"\.([\w-]+)\s*\{([^}]+)\}", element.text or ""):
            styles[name] = dict(
                (key.strip(), value.strip())
                for key, value in (part.split(":", 1) for part in body.split(";") if ":" in part)
            )
    for element in root.iter():
        for name in element.get("class", "").split():
            for key, value in styles.get(name, {}).items():
                if key not in element.attrib and not (key == "stroke-width" and value == "none"):
                    element.set(key, value)


def render_dxf_svg(
    source: Path,
    destination: Path,
    *,
    bounds: list[float] | None = None,
    linearize_curves: bool = True,
    include_invisible: bool = False,
    white_background: bool = False,
) -> dict:
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, svg
    from ezdxf.fonts import fonts
    from ezdxf.math import BoundingBox2d

    document = ezdxf.readfile(source)
    audit = document.audit()
    if audit.errors:
        raise ValueError(f"DXF has {len(audit.errors)} unrecoverable audit errors")

    class TextRenderContext(RenderContext):
        def __init__(self, doc, *, include_invisible_entities: bool = False):
            super().__init__(doc)
            self.cjk_fallbacks = set()
            self.include_invisible_entities = bool(include_invisible_entities)

        def resolve_all(self, entity):
            properties = super().resolve_all(entity)
            # Diagnostic-only escape hatch for Tianzheng anonymous/dynamic
            # blocks.  Some T3-to-DXF saves carry multiple visibility states
            # as ordinary DXF entities marked ``invisible``.  Rendering all
            # of them lets the caller prove whether missing geometry was
            # removed by visibility filtering before changing the production
            # visibility policy.
            if self.include_invisible_entities:
                properties.is_visible = True
            return properties

        def resolve_font(self, entity):
            text = entity.dxf.get("text", "")
            if any("\u3400" <= char <= "\u9fff" for char in text):
                # T3 TEXT may retain Standard/txt.shx, which has no CJK glyphs.
                # Change the display font only; source text/placement stay intact.
                self.cjk_fallbacks.add((entity.dxf.handle, text))
                return fonts.get_font_face("simhei.ttf")
            return super().resolve_font(entity)

    class TaggedSVGRenderer(svg.SVGRenderBackend):
        def _tag_last(self, properties):
            self.entities[-1].set("data-cad-layer", properties.layer)
            self.entities[-1].set("data-cad-handle", properties.handle)

        def add_strokes(self, d, properties):
            if d:
                super().add_strokes(d, properties)
                self._tag_last(properties)

        def add_filling(self, d, properties):
            if d:
                super().add_filling(d, properties)
                self._tag_last(properties)

    class TaggedSVGBackend(svg.SVGBackend):
        @staticmethod
        def make_backend(page, settings):
            return TaggedSVGRenderer(page, settings)

    class AuditedFrontend(Frontend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.drawn = Counter()
            self.layers = Counter()
            self.skipped = Counter()
            self.blocks = []

        def draw_entity(self, entity, properties):
            self.drawn[entity.dxftype()] += 1
            self.layers[properties.layer] += 1
            if entity.dxftype() == "INSERT":
                self.blocks.append({"handle": entity.dxf.handle, "name": entity.dxf.name,
                                    "layer": properties.layer})
            super().draw_entity(entity, properties)

        def skip_entity(self, entity, msg):
            self.skipped[f"{entity.dxftype()}: {msg}"] += 1

    backend = TaggedSVGBackend()
    configuration = config.Configuration(background_policy=(
        config.BackgroundPolicy.WHITE if white_background else config.BackgroundPolicy.BLACK))
    context = TextRenderContext(document, include_invisible_entities=include_invisible)
    frontend = AuditedFrontend(context, backend, config=configuration)
    frontend.draw_layout(document.modelspace())
    render_box = BoundingBox2d([(bounds[0], bounds[1]), (bounds[2], bounds[3])]) if bounds else None
    box = render_box or backend.player().bbox()
    page = layout.Page(420, 420 * box.size.y / box.size.x)
    settings = layout.Settings(output_coordinate_space=100000, crop_at_margins=bool(bounds))
    root = backend.get_xml_root_element(page, settings=settings, render_box=render_box)
    inline_svg_styles(root)
    # ezdxf expands dashed strokes into SVG paths.  A dash that is fully
    # clipped at a boundary can become a path containing only zero-length
    # segments.  It is harmless for raster display, but svgpathtools (and the
    # SymPointV2 v5 preprocessor) cannot sample such a path.  Remove only
    # paths with no geometric length; real furniture/window linework remains
    # untouched.
    try:
        from svgpathtools import parse_path

        parents = {child: parent for parent in root.iter() for child in parent}
        primitive_index = 0
        preserved_curve_segments = 0
        linearized_curve_segments = 0
        converted_circles = 0
        converted_ellipses = 0
        for element in list(root.iter()):
            if element.tag.rsplit("}", 1)[-1] != "path":
                continue
            path_data = element.get("d")
            if not path_data:
                continue
            try:
                parsed = parse_path(path_data)
                if float(parsed.length()) <= 1e-9:
                    parent = parents.get(element)
                    if parent is not None:
                        parent.remove(element)
                    continue

                # The SVG backend writes circles and other curved entities as
                # cubic/quadratic Bézier paths.  SymPointV2 v5 accepts only
                # line/arc/circle/ellipse paths.  The default compatibility
                # mode preserves the visible shape with a deterministic
                # polyline approximation.  A diagnostic caller can disable
                # this rewrite to inspect the backend's native SVG command
                # distribution before choosing a curve-preserving adapter.
                if not linearize_curves:
                    element.set("data-primitive-id", f"t3_svg_primitive_{primitive_index:06d}")
                    primitive_index += 1
                    continue
                ellipse = _full_ellipse(parsed)
                if ellipse is not None:
                    center_x, center_y, radius_x, radius_y, _residual = ellipse
                    element.tag = "{http://www.w3.org/2000/svg}circle" if abs(radius_x - radius_y) / max(radius_x, radius_y) <= 0.003 else "{http://www.w3.org/2000/svg}ellipse"
                    element.attrib.pop("d", None)
                    element.set("cx", f"{center_x:.12g}")
                    element.set("cy", f"{center_y:.12g}")
                    if element.tag.endswith("circle"):
                        element.set("r", f"{(radius_x + radius_y) / 2.0:.12g}")
                        converted_circles += 1
                    else:
                        element.set("rx", f"{radius_x:.12g}")
                        element.set("ry", f"{radius_y:.12g}")
                        converted_ellipses += 1
                    element.set("data-primitive-id", f"t3_svg_primitive_{primitive_index:06d}")
                    primitive_index += 1
                    continue
                commands: list[str] = []
                previous_end = None
                for segment in parsed:
                    start = complex(segment.start)
                    end = complex(segment.end)
                    if abs(end - start) <= 1e-9:
                        continue
                    if previous_end is None or abs(start - previous_end) > 1e-7:
                        commands.append(f"M {start.real:.12g},{start.imag:.12g}")
                    if segment.__class__.__name__ == "Line":
                        commands.append(f"L {end.real:.12g},{end.imag:.12g}")
                        previous_end = end
                        continue
                    circular = _circular_curve(segment)
                    if circular is not None and circular[2] <= 0.003 and abs(end - start) > 1e-7:
                        center, radius, _residual = circular
                        middle = complex(segment.point(0.5))
                        large_arc, sweep = _arc_flags(start, middle, end, radius)
                        commands.append(
                            f"A {radius:.12g},{radius:.12g} 0 {large_arc} {sweep} "
                            f"{end.real:.12g},{end.imag:.12g}"
                        )
                        preserved_curve_segments += 1
                    else:
                        sample_count = 8
                        for sample in range(1, sample_count + 1):
                            point = complex(segment.point(sample / sample_count))
                            commands.append(f"L {point.real:.12g},{point.imag:.12g}")
                        linearized_curve_segments += 1
                    previous_end = end
                if not commands:
                    parent = parents.get(element)
                    if parent is not None:
                        parent.remove(element)
                    continue
                element.set("d", " ".join(commands))
                element.set("data-primitive-id", f"t3_svg_primitive_{primitive_index:06d}")
                primitive_index += 1
            except Exception:
                # Keep malformed paths for the normal renderer error path;
                # silently dropping potentially meaningful CAD geometry is
                # worse than surfacing a parse failure.
                continue
    except ImportError:
        # svgpathtools is already a project dependency for local v5 parsing,
        # but leave the exporter usable in a minimal display-only install.
        pass
    root.set("data-source", source.name)
    root.set("data-renderer", "ezdxf-visible-blocks")
    root.set("data-world-bounds", ",".join(str(x) for x in [box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
    return {
        "source": str(source.resolve()), "svg": str(destination.resolve()),
        "renderer": f"ezdxf {ezdxf.__version__} SVGBackend",
        "audit_errors": len(audit.errors), "audit_fixes": len(audit.fixes),
        "model_entities": dict(Counter(e.dxftype() for e in document.modelspace())),
        "drawn_entity_types": dict(frontend.drawn), "drawn_layers": dict(frontend.layers),
        "drawn_blocks": frontend.blocks, "skipped": dict(frontend.skipped),
        "all_block_definition_invisible_entities": sum(
            bool(e.dxf.get("invisible", 0)) for block in document.blocks for e in block
            if e.dxf.is_supported("invisible")),
        "cjk_display_font": "simhei.ttf", "cjk_fallback_texts": len(context.cjk_fallbacks),
        "world_bounds": [box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y],
        "svg_paths": sum(e.tag.rsplit("}", 1)[-1] == "path" for e in root.iter()),
        "svg_use_references": sum(e.tag.rsplit("}", 1)[-1] == "use" for e in root.iter()),
        "svg_paths_by_layer": dict(Counter(
            e.get("data-cad-layer", "") for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "path")),
        "recognition_run": False,
        "linearize_curves": bool(linearize_curves),
        "include_invisible": bool(include_invisible),
        "white_background": bool(white_background),
        "preserved_curve_segments": preserved_curve_segments if linearize_curves else 0,
        "linearized_curve_segments": linearized_curve_segments if linearize_curves else 0,
        "converted_circles": converted_circles if linearize_curves else 0,
        "converted_ellipses": converted_ellipses if linearize_curves else 0,
        "svg_circles": sum(e.tag.rsplit("}", 1)[-1] == "circle" for e in root.iter()),
        "svg_ellipses": sum(e.tag.rsplit("}", 1)[-1] == "ellipse" for e in root.iter()),
    }
