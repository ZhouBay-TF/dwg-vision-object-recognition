from __future__ import annotations

import re
from pathlib import Path

import fitz
from PIL import Image, ImageDraw


ROOT = Path(r"E:\Code\dwgLLM")
SVG = ROOT / ".tools" / "sample.svg"
OUT = ROOT / ".tools" / "crops"
OUT.mkdir(parents=True, exist_ok=True)

svg = SVG.read_text(encoding="utf-8")

regions = [
    ("row1", 5000, 18000, 68000, 22000),
    ("row2", 5000, 73000, 68000, 22000),
    ("row3", 5000, 144000, 68000, 22000),
    ("row4", 5000, 201000, 68000, 22000),
]

for name, x, y, w, h in regions:
    cropped = re.sub(r'viewBox="[^"]+"', f'viewBox="{x} {y} {w} {h}"', svg, count=1)
    cropped = re.sub(r'width="[^"]+"', 'width="2400"', cropped, count=1)
    cropped = re.sub(r'height="[^"]+"', 'height="800"', cropped, count=1)
    svg_path = OUT / f"{name}.svg"
    png_path = OUT / f"{name}.png"
    svg_path.write_text(cropped, encoding="utf-8")
    doc = fitz.open(stream=cropped.encode("utf-8"), filetype="svg")
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
    pix.save(str(png_path))
    doc.close()
    print(name, png_path, Image.open(png_path).size)

# A compact contact sheet for fast inspection.
images = [Image.open(OUT / f"row{i}.png").convert("RGB") for i in range(1, 5)]
thumb_w = 1600
thumbs = []
for im in images:
    scale = thumb_w / im.width
    thumbs.append(im.resize((thumb_w, round(im.height * scale))))
sheet = Image.new("RGB", (thumb_w, sum(im.height for im in thumbs)), "white")
draw = ImageDraw.Draw(sheet)
y0 = 0
for i, im in enumerate(thumbs, 1):
    sheet.paste(im, (0, y0))
    draw.rectangle((0, y0, 120, y0 + 34), fill="white")
    draw.text((8, y0 + 8), f"row{i}", fill="black")
    y0 += im.height
sheet.save(OUT / "contact_sheet.png")
print("contact", OUT / "contact_sheet.png", sheet.size)
