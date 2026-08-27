from __future__ import annotations

import re
from pathlib import Path

import fitz


ROOT = Path(r"E:\Code\dwgLLM")
SVG = ROOT / "runs" / "reload_check" / "建筑方案图_1_10112_131360a6.sv$.svg"
OUT = ROOT / "runs" / "reload_check" / "focus"
OUT.mkdir(parents=True, exist_ok=True)

svg = SVG.read_text(encoding="utf-8")

regions = [
    ("left_bathrooms", 7000, 20500, 12500, 15000),
    ("middle_left_bathrooms", 30000, 20500, 19000, 15000),
    ("middle_right_bathrooms", 39000, 20500, 10500, 15000),
    ("right_bathrooms", 59000, 20500, 11000, 15000),
    ("lower_rooms", 7000, 25500, 65000, 9000),
    ("row1_extra_label", 9000, 27500, 8000, 8000),
    ("row2_same_position", 9000, 82500, 8000, 8000),
]

for name, x, y, w, h in regions:
    cropped = re.sub(r'viewBox="[^"]+"', f'viewBox="{x} {y} {w} {h}"', svg, count=1)
    cropped = re.sub(r'width="[^"]+"', 'width="2400"', cropped, count=1)
    cropped = re.sub(r'height="[^"]+"', 'height="1200"', cropped, count=1)
    doc = fitz.open(stream=cropped.encode("utf-8"), filetype="svg")
    try:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pix.save(str(OUT / f"{name}.png"))
    finally:
        doc.close()
    print(OUT / f"{name}.png")
