from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Iterable

from PIL import Image


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _resize_to_max(image: Image.Image, max_dimension: int) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size
    scale = min(1.0, max_dimension / max(width, height))
    if scale == 1.0:
        return image
    return image.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS)


def load_images(source: Path, dpi: int = 300) -> list[tuple[str, Image.Image]]:
    """Load an image or render every page of a PDF into RGB images."""
    suffix = source.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            return [(source.stem, Image.open(source).convert("RGB"))]

    if suffix == ".pdf":
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover - dependency message
            raise RuntimeError("读取 PDF 需要安装 PyMuPDF") from exc

        pages: list[tuple[str, Image.Image]] = []
        document = fitz.open(source)
        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        for index, page in enumerate(document):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            pages.append((f"{source.stem}_page_{index + 1:03d}", image))
        return pages

    raise ValueError(
        f"暂不支持直接读取 {source.suffix}。请先渲染为 PNG/JPEG/PDF，或使用 --oda-exe 处理 DWG。"
    )


def create_tile_manifest(
    source: Path,
    output_dir: Path,
    *,
    tile_size: int = 3072,
    overlap: int = 384,
    overview_max: int = 6000,
    dpi: int = 300,
    mosaic_group_size: int = 4,
    mosaic_max_dimension: int = 8192,
    write_preprocess: bool = True,
) -> Path:
    """Create an overview, overlapping tiles, mosaics, and a coordinate manifest.

    A mosaic contains several original tiles at the largest common scale that
    fits the configured canvas.  The manifest records each panel's transform,
    so model boxes can be mapped back to the full drawing without guessing.
    """
    if tile_size <= 0 or overlap < 0 or overlap >= tile_size:
        raise ValueError("tile_size 必须大于 0，overlap 必须满足 0 <= overlap < tile_size")
    if mosaic_group_size <= 0 or mosaic_max_dimension <= 0:
        raise ValueError("mosaic_group_size 和 mosaic_max_dimension 必须大于 0")

    output_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = output_dir / "pages"
    tiles_dir = output_dir / "tiles"
    pages_dir.mkdir(exist_ok=True)
    tiles_dir.mkdir(exist_ok=True)

    page_manifests: list[dict] = []
    for page_name, original in load_images(source, dpi=dpi):
        image = original.convert("RGB")
        page_path = pages_dir / f"{page_name}.png"
        image.save(page_path, format="PNG", optimize=True)

        overview = _resize_to_max(image, overview_max)
        overview_path = pages_dir / f"{page_name}_overview.png"
        overview.save(overview_path, format="PNG", optimize=True)

        width, height = image.size
        regions: list[dict] = []
        if write_preprocess:
            try:
                from .preprocess import write_preprocess_artifacts

                regions = write_preprocess_artifacts(image, output_dir / "preprocess" / page_name)
            except RuntimeError:
                # OpenCV is an optional runtime dependency.  The core tiler
                # remains usable if it is intentionally omitted.
                regions = []
        step = tile_size - overlap
        tiles: list[dict] = []
        y_positions = list(range(0, max(height - tile_size, 0) + 1, step))
        x_positions = list(range(0, max(width - tile_size, 0) + 1, step))
        if not y_positions or y_positions[-1] + tile_size < height:
            y_positions.append(max(height - tile_size, 0))
        if not x_positions or x_positions[-1] + tile_size < width:
            x_positions.append(max(width - tile_size, 0))

        seen: set[tuple[int, int]] = set()
        for row, y in enumerate(y_positions):
            for column, x in enumerate(x_positions):
                if (x, y) in seen:
                    continue
                seen.add((x, y))
                actual_width = min(tile_size, width - x)
                actual_height = min(tile_size, height - y)
                crop = image.crop((x, y, x + actual_width, y + actual_height))

                # Pad edge tiles to a stable size so the visual input has a predictable layout.
                if crop.size != (tile_size, tile_size):
                    padded = Image.new("RGB", (tile_size, tile_size), "white")
                    padded.paste(crop, (0, 0))
                    crop = padded

                tile_id = f"{page_name}_r{row:03d}_c{column:03d}"
                tile_path = tiles_dir / f"{tile_id}.png"
                crop.save(tile_path, format="PNG", optimize=True)
                tiles.append(
                    {
                        "tile_id": tile_id,
                        "path": tile_path.relative_to(output_dir).as_posix(),
                        "x": x,
                        "y": y,
                        "width": actual_width,
                        "height": actual_height,
                        "canvas_width": tile_size,
                        "canvas_height": tile_size,
                        "row": row,
                        "column": column,
                    }
                )

        mosaics = _create_mosaics(
            output_dir=output_dir,
            page_name=page_name,
            tiles=tiles,
            tiles_dir=tiles_dir,
            group_size=mosaic_group_size,
            max_dimension=mosaic_max_dimension,
        )
        page_manifests.append(
            {
                "page_id": page_name,
                "source_path": page_path.relative_to(output_dir).as_posix(),
                "overview_path": overview_path.relative_to(output_dir).as_posix(),
                "width": width,
                "height": height,
                "regions": regions,
                "tiles": tiles,
                "mosaics": mosaics,
            }
        )

    manifest = {
        "schema_version": "0.2",
        "source": str(source.resolve()),
        "tile_size": tile_size,
        "overlap": overlap,
        "pages": page_manifests,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _create_mosaics(
    *,
    output_dir: Path,
    page_name: str,
    tiles: list[dict],
    tiles_dir: Path,
    group_size: int,
    max_dimension: int,
) -> list[dict]:
    """Pack tiles into readable contact sheets with reversible panel mapping."""
    from PIL import ImageDraw

    mosaic_dir = output_dir / "mosaics"
    mosaic_dir.mkdir(parents=True, exist_ok=True)
    mosaics: list[dict] = []
    gap = 32
    columns = max(1, round(group_size**0.5))
    rows = (group_size + columns - 1) // columns

    for batch_start in range(0, len(tiles), group_size):
        batch = tiles[batch_start : batch_start + group_size]
        if not batch:
            continue
        base_width = max(int(tile["canvas_width"]) for tile in batch)
        base_height = max(int(tile["canvas_height"]) for tile in batch)
        natural_width = columns * base_width + (columns - 1) * gap
        natural_height = rows * base_height + (rows - 1) * gap
        scale = min(1.0, max_dimension / max(natural_width, natural_height))
        panel_width = max(1, round(base_width * scale))
        panel_height = max(1, round(base_height * scale))
        canvas_width = min(max_dimension, columns * panel_width + (columns - 1) * gap)
        canvas_height = min(max_dimension, rows * panel_height + (rows - 1) * gap)
        canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
        draw = ImageDraw.Draw(canvas)
        panels: list[dict] = []

        for index, tile in enumerate(batch):
            tile_path = tiles_dir / Path(tile["path"]).name
            panel = Image.open(tile_path).convert("RGB")
            if panel.size != (panel_width, panel_height):
                panel = panel.resize((panel_width, panel_height), Image.Resampling.LANCZOS)
            column = index % columns
            row = index // columns
            panel_x = column * (panel_width + gap)
            panel_y = row * (panel_height + gap)
            canvas.paste(panel, (panel_x, panel_y))
            draw.rectangle(
                (panel_x, panel_y, panel_x + panel_width - 1, panel_y + panel_height - 1),
                outline=(220, 40, 40),
                width=max(1, round(2 * scale)),
            )
            panels.append(
                {
                    "panel_id": tile["tile_id"],
                    "tile_id": tile["tile_id"],
                    "x": panel_x,
                    "y": panel_y,
                    "width": panel_width,
                    "height": panel_height,
                    "scale": scale,
                    "global_x": tile["x"],
                    "global_y": tile["y"],
                    "global_width": tile["width"],
                    "global_height": tile["height"],
                }
            )

        mosaic_id = f"{page_name}_m{batch_start // group_size:03d}"
        mosaic_path = mosaic_dir / f"{mosaic_id}.png"
        canvas.save(mosaic_path, format="PNG", optimize=True)
        mosaics.append(
            {
                "mosaic_id": mosaic_id,
                "path": mosaic_path.relative_to(output_dir).as_posix(),
                "width": canvas_width,
                "height": canvas_height,
                "panels": panels,
            }
        )
    return mosaics
