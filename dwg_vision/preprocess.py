from __future__ import annotations

"""OpenCV helpers used before a drawing is sent to a vision model.

These operations are intentionally conservative: they create evidence images
and region hints, but the original raster is always preserved and remains the
model input.  A thresholded image is never treated as ground truth geometry.
"""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class DrawingRegion:
    left: int
    top: int
    right: int
    bottom: int
    ink_ratio: float
    area_ratio: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "bbox_px": [self.left, self.top, self.right, self.bottom],
            "ink_ratio": round(self.ink_ratio, 6),
            "area_ratio": round(self.area_ratio, 6),
        }


def _cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - optional deployment path
        raise RuntimeError("OpenCV 预处理需要安装 opencv-python") from exc
    return cv2


def build_ink_mask(image: Image.Image) -> Image.Image:
    """Build a readable black/white mask without changing the source image."""
    cv2 = _cv2()
    gray = np.asarray(image.convert("L"))
    # Adaptive threshold survives faint CAD lines and uneven scanned backgrounds.
    mask = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        8,
    )
    # Close small gaps in walls and outlines, while keeping text as evidence.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    return Image.fromarray(255 - mask).convert("L")


def detect_drawing_regions(image: Image.Image, *, min_area_ratio: float = 0.002) -> list[DrawingRegion]:
    """Find large ink-bearing regions for focus crops and mosaic grouping."""
    cv2 = _cv2()
    gray = np.asarray(image.convert("L"))
    _, binary = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY_INV)
    height, width = binary.shape[:2]
    if width == 0 or height == 0:
        return []

    # Joining close CAD linework creates a region around a plan instead of one
    # component per glyph/line.  The kernel scales with the image size.
    join = max(5, min(101, round(min(width, height) / 180)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (join, join))
    joined = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    joined = cv2.dilate(joined, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)), iterations=1)
    contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    regions: list[DrawingRegion] = []
    image_area = float(width * height)
    for contour in contours:
        left, top, region_width, region_height = cv2.boundingRect(contour)
        area = region_width * region_height
        if area / image_area < min_area_ratio:
            continue
        crop = binary[top : top + region_height, left : left + region_width]
        ink_ratio = float(np.count_nonzero(crop)) / max(1, crop.size)
        regions.append(
            DrawingRegion(
                left=left,
                top=top,
                right=left + region_width,
                bottom=top + region_height,
                ink_ratio=ink_ratio,
                area_ratio=area / image_area,
            )
        )

    regions.sort(key=lambda item: item.area_ratio, reverse=True)
    # Remove nested regions; nested contours are usually a title box or a
    # thick outline inside the same drawing rather than another plan.
    selected: list[DrawingRegion] = []
    for region in regions:
        if any(
            region.left >= other.left
            and region.top >= other.top
            and region.right <= other.right
            and region.bottom <= other.bottom
            for other in selected
        ):
            continue
        selected.append(region)
    return selected


def write_preprocess_artifacts(image: Image.Image, output_dir: Path) -> list[dict[str, Any]]:
    """Write optional audit images and return region metadata."""
    output_dir.mkdir(parents=True, exist_ok=True)
    mask = build_ink_mask(image)
    mask_path = output_dir / "ink_mask.png"
    mask.save(mask_path, format="PNG", optimize=True)
    regions = detect_drawing_regions(image)
    (output_dir / "regions.json").write_text(
        json.dumps([region.as_dict() for region in regions], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result: list[dict[str, Any]] = []
    width, height = image.size
    for index, region in enumerate(regions[:16]):
        margin = max(16, round(max(region.right - region.left, region.bottom - region.top) * 0.04))
        left = max(0, region.left - margin)
        top = max(0, region.top - margin)
        right = min(width, region.right + margin)
        bottom = min(height, region.bottom + margin)
        focus_path = output_dir / f"focus_r{index:03d}.png"
        image.crop((left, top, right, bottom)).save(focus_path, format="PNG", optimize=True)
        metadata = region.as_dict()
        metadata["focus_path"] = focus_path.name
        metadata["focus_bbox_px"] = [left, top, right, bottom]
        result.append(metadata)
    # Keep regions.json consistent with the returned manifest metadata.
    (output_dir / "regions.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result
