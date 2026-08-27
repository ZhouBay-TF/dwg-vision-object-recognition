from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import math
from pathlib import Path


def convert_dwg_to_dxf(
    dwg_path: Path,
    output_dir: Path,
    *,
    oda_exe: str,
    oda_version: str = "ACAD2018",
) -> Path:
    """Convert one DWG to DXF with the folder-based ODA File Converter CLI."""
    output_dir.mkdir(parents=True, exist_ok=True)
    dxf_dir = output_dir / "dxf"
    dxf_dir.mkdir(exist_ok=True)

    command = [
        oda_exe,
        str(dwg_path.parent),
        str(dxf_dir),
        oda_version,
        "DXF",
        "0",
        "1",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"DWG 转 DXF 失败，退出码 {completed.returncode}: {details}")

    candidates = sorted(dxf_dir.glob(f"{dwg_path.stem}*.dxf"))
    if not candidates:
        candidates = sorted(dxf_dir.glob("*.dxf"))
    if not candidates:
        raise RuntimeError(
            f"ODA 转换器运行完成，但在 {dxf_dir} 没有找到 DXF。"
        )
    return candidates[0]


def find_libredwg_exe(explicit: str | None = None) -> Path | None:
    """Find the local LibreDWG SVG exporter, if one is available."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))

    project_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            project_root / ".tools" / "libredwg-win64" / "dwg2SVG.exe",
            project_root / ".tools" / "libredwg" / "dwg2SVG.exe",
        ]
    )
    on_path = shutil.which("dwg2SVG") or shutil.which("dwg2SVG.exe")
    if on_path:
        candidates.append(Path(on_path))

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def convert_dwg_to_svg(
    dwg_path: Path,
    output_svg: Path,
    *,
    libredwg_exe: str | None = None,
    mspace: bool = True,
) -> Path:
    """Convert DWG to SVG with the local LibreDWG command-line exporter."""
    executable = find_libredwg_exe(libredwg_exe)
    if executable is None:
        raise FileNotFoundError(
            "找不到 LibreDWG dwg2SVG.exe。请通过 --libredwg-exe 指定，"
            "或把它放到 .tools/libredwg-win64/。"
        )

    command = [str(executable)]
    if mspace:
        command.append("--mspace")
    command.append(str(dwg_path))
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"LibreDWG 转 SVG 失败，退出码 {completed.returncode}: {details}"
        )
    if not completed.stdout.lstrip().startswith(b"<?xml") and b"<svg" not in completed.stdout[:4096]:
        details = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"LibreDWG 没有输出有效 SVG。{details}")

    output_svg.parent.mkdir(parents=True, exist_ok=True)
    output_svg.write_bytes(completed.stdout)
    if completed.stderr:
        output_svg.with_suffix(output_svg.suffix + ".warnings.txt").write_text(
            completed.stderr.decode("utf-8", errors="replace"), encoding="utf-8"
        )
    return output_svg


def _svg_viewbox_from_bathroom_labels(svg_text: str) -> tuple[float, float, float, float] | None:
    """Focus an exported drawing on its bathroom-label regions when available."""
    root = ET.fromstring(svg_text)
    labels: list[tuple[float, float]] = []
    for element in root.iter():
        if not element.tag.endswith("text"):
            continue
        text = "".join(element.itertext()).strip()
        if text != "卫生间":
            continue
        try:
            labels.append((float(element.attrib["x"]), float(element.attrib["y"])))
        except (KeyError, ValueError):
            continue
    if not labels:
        return None

    x_values = [point[0] for point in labels]
    y_values = [point[1] for point in labels]
    left = min(x_values) - 9000
    top = min(y_values) - 9000
    right = max(x_values) + 9000
    bottom = max(y_values) + 12000
    return left, top, right - left, bottom - top


def render_svg_to_png(
    svg_path: Path,
    output_png: Path,
    *,
    max_dimension: int = 16000,
    viewbox: tuple[float, float, float, float] | None = None,
    auto_focus: bool = True,
) -> Path:
    """Rasterize SVG locally; optional legacy bathroom auto-crop."""
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - dependency message
        raise RuntimeError("SVG 渲染需要安装 PyMuPDF") from exc

    svg_text = svg_path.read_text(encoding="utf-8")
    if viewbox is None and auto_focus:
        viewbox = _svg_viewbox_from_bathroom_labels(svg_text)

    root = ET.fromstring(svg_text)
    if viewbox is not None:
        x, y, width, height = viewbox
        root.set("viewBox", f"{x:g} {y:g} {width:g} {height:g}")
    else:
        raw_viewbox = root.attrib.get("viewBox", "")
        values = [float(value) for value in re.findall(r"[-+]?\d+(?:\.\d+)?", raw_viewbox)]
        if len(values) != 4:
            raise RuntimeError("SVG 缺少有效 viewBox，无法确定高清渲染范围")
        _, _, width, height = values

    if width <= 0 or height <= 0:
        raise RuntimeError("SVG viewBox 的宽高必须为正数")
    scale = max_dimension / max(width, height)
    pixel_width = max(1, round(width * scale))
    pixel_height = max(1, round(height * scale))
    root.set("width", str(pixel_width))
    root.set("height", str(pixel_height))
    raster_svg = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    document = fitz.open(stream=raster_svg, filetype="svg")
    try:
        pixmap = document[0].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        output_png.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(output_png)
    finally:
        document.close()
    return output_png


def render_dxf_to_png(
    dxf_path: Path,
    output_png: Path,
    *,
    dpi: int = 300,
    layout_name: str = "Model",
    max_dimension: int = 16000,
) -> Path:
    """Render DXF locally using ezdxf's MuPDF backend with an OOM guard.

    DXF drawings often use millimetres as drawing units.  Passing 300 DPI to
    a site plan measured in hundreds of thousands of millimetres can request
    a multi-terabyte bitmap.  The effective DPI is therefore reduced only
    when the estimated longest side would exceed ``max_dimension``.
    """
    try:
        import ezdxf
        from ezdxf import bbox as dxf_bbox
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
        from ezdxf.addons.drawing.file_output import MuPDFFileOutput
    except ImportError as exc:  # pragma: no cover - dependency message
        raise RuntimeError("DXF 渲染需要安装 ezdxf 和 PyMuPDF") from exc

    output_png.parent.mkdir(parents=True, exist_ok=True)
    document = ezdxf.readfile(dxf_path)
    try:
        layout = document.layouts.get(layout_name)
    except KeyError as exc:
        available = ", ".join(layout.name for layout in document.layouts)
        raise RuntimeError(f"找不到布局 {layout_name!r}，可用布局：{available}") from exc

    effective_dpi = max(1, int(dpi))
    try:
        extents = dxf_bbox.extents(layout)
        largest_units = max(float(extents.size.x), float(extents.size.y))
        insunits = int(document.header.get("$INSUNITS", 0) or 0)
        unit_to_inches = {
            1: 1.0,
            2: 12.0,
            4: 1.0 / 25.4,
            5: 10.0 / 25.4,
            6: 1000.0 / 25.4,
        }.get(insunits, 1.0 / 25.4)
        estimated_pixels = largest_units * unit_to_inches * effective_dpi
        if estimated_pixels > max_dimension:
            effective_dpi = max(1, math.floor(effective_dpi * max_dimension / estimated_pixels))
    except Exception:
        # A conservative low DPI is preferable to a process-wide allocation
        # failure when a proxy object has no usable extents.
        effective_dpi = min(effective_dpi, 96)

    file_output = MuPDFFileOutput(effective_dpi)
    config = Configuration().with_changes(background_policy=BackgroundPolicy.WHITE)
    frontend = Frontend(RenderContext(document), file_output.backend(), config=config)
    frontend.draw_layout(layout, finalize=True)
    file_output.save(output_png)
    return output_png


def prepare_visual_source(
    input_path: Path,
    output_dir: Path,
    *,
    oda_exe: str | None = None,
    libredwg_exe: str | None = None,
    oda_version: str = "ACAD2018",
    dpi: int = 300,
    layout_name: str = "Model",
    svg_max_dimension: int = 16000,
    raster_max_dimension: int = 16000,
    auto_focus: bool = True,
) -> Path:
    """Return a vision-friendly source path, rendering DWG/DXF when needed."""
    suffix = input_path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf"}:
        return input_path

    output_dir.mkdir(parents=True, exist_ok=True)
    if suffix == ".svg":
        return render_svg_to_png(
            input_path,
            output_dir / "rendered.png",
            max_dimension=svg_max_dimension,
            auto_focus=auto_focus,
        )

    if suffix == ".dwg":
        local_libredwg = find_libredwg_exe(libredwg_exe)
        if local_libredwg:
            svg_path = convert_dwg_to_svg(
                input_path,
                output_dir / f"{input_path.stem}.svg",
                libredwg_exe=str(local_libredwg),
            )
            return render_svg_to_png(
                svg_path,
                output_dir / "rendered.png",
                max_dimension=svg_max_dimension,
                auto_focus=auto_focus,
            )
        if not oda_exe:
            raise RuntimeError(
                "处理 DWG 需要 LibreDWG dwg2SVG.exe 或 ODAFileConverter。"
                "请通过 --libredwg-exe/--oda-exe 指定。"
            )
        dxf_path = convert_dwg_to_dxf(
            input_path,
            output_dir,
            oda_exe=oda_exe,
            oda_version=oda_version,
        )
        return render_dxf_to_png(
            dxf_path,
            output_dir / "rendered.png",
            dpi=dpi,
            layout_name=layout_name,
            max_dimension=raster_max_dimension,
        )

    if suffix == ".dxf":
        return render_dxf_to_png(
            input_path,
            output_dir / "rendered.png",
            dpi=dpi,
            layout_name=layout_name,
            max_dimension=raster_max_dimension,
        )

    raise ValueError(f"不支持的输入格式：{input_path.suffix}")
