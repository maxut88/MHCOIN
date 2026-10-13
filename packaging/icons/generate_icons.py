#!/usr/bin/env python3
"""Regenerate MHCOIN app icons (PNG / ICO / ICNS) from the coin mark.

Run from repo root:
  python3 packaging/icons/generate_icons.py
"""
from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mhcoin" / "desktop" / "assets"
ICONS = Path(__file__).resolve().parent


def render_coin(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    pad = max(1, s // 16)
    gd.ellipse((pad, pad, s - 1 - pad, s - 1 - pad), fill=(45, 212, 160, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(1, s // 18)))
    img = Image.alpha_composite(img, glow)

    m = max(1, s // 18)
    for i, col in enumerate(
        [
            (93, 255, 192, 230),
            (45, 212, 160, 255),
            (26, 168, 118, 255),
            (15, 122, 85, 255),
        ]
    ):
        inset = m + int((s * 0.02) * i)
        overlay = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).ellipse(
            (inset, inset, s - 1 - inset, s - 1 - inset), fill=col
        )
        img = Image.alpha_composite(img, overlay)

    hi = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    hd.ellipse(
        (int(s * 0.18), int(s * 0.14), int(s * 0.72), int(s * 0.55)),
        fill=(255, 255, 255, 70),
    )
    hi = hi.filter(ImageFilter.GaussianBlur(radius=max(1, s // 14)))
    img = Image.alpha_composite(img, hi)

    d = ImageDraw.Draw(img)
    font = None
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if Path(name).is_file():
            try:
                font = ImageFont.truetype(name, size=max(10, int(s * 0.48)))
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()

    bbox = d.textbbox((0, 0), "M", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (s - tw) / 2 - bbox[0]
    y = (s - th) / 2 - bbox[1] - s * 0.02
    d.text((x, y + max(1, s // 64)), "M", font=font, fill=(0, 38, 26, 90))
    d.text((x, y), "M", font=font, fill=(6, 38, 26, 255))
    rim = max(1, s // 40)
    d.ellipse((m, m, s - 1 - m, s - 1 - m), outline=(45, 212, 160, 180), width=rim)
    return img


def write_icns(path: Path, images: dict[int, Image.Image]) -> None:
    order = [
        ("icp4", 16),
        ("icp5", 32),
        ("icp6", 64),
        ("ic07", 128),
        ("ic08", 256),
        ("ic09", 512),
        ("ic10", 1024),
        ("ic11", 32),
        ("ic12", 64),
        ("ic13", 256),
        ("ic14", 512),
    ]
    chunks: list[bytes] = []
    for tag, px in order:
        im = images.get(px)
        if im is None:
            continue
        if im.size != (px, px):
            im = im.resize((px, px), Image.Resampling.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="PNG")
        data = buf.getvalue()
        chunks.append(tag.encode("ascii") + struct.pack(">I", 8 + len(data)) + data)
    body = b"".join(chunks)
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    ICONS.mkdir(parents=True, exist_ok=True)
    sizes = [16, 32, 48, 64, 128, 256, 512, 1024]
    rendered = {sz: render_coin(sz) for sz in sizes}

    rendered[512].save(ASSETS / "mhcoin.png")
    rendered[256].save(ASSETS / "mhcoin-256.png")
    rendered[64].save(ASSETS / "mhcoin-64.png")
    rendered[32].save(ASSETS / "mhcoin-32.png")
    rendered[512].save(ICONS / "mhcoin.png")
    rendered[256].save(ICONS / "mhcoin-256.png")
    rendered[512].save(ICONS / "mhcoin-512.png")

    ico_sizes = [(sz, sz) for sz in (16, 32, 48, 64, 128, 256)]
    rendered[256].save(ICONS / "mhcoin.ico", format="ICO", sizes=ico_sizes)
    rendered[256].save(ASSETS / "mhcoin.ico", format="ICO", sizes=ico_sizes)
    write_icns(ICONS / "mhcoin.icns", rendered)
    write_icns(ASSETS / "mhcoin.icns", rendered)
    print(f"icons → {ICONS}")
    print(f"assets → {ASSETS}")


if __name__ == "__main__":
    main()
