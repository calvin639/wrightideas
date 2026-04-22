#!/usr/bin/env python3
"""
Generate a QR code SVG (and PNG) locally from a URL.
Usage: python scripts/generate_qr.py <url> [output_name]

Examples:
  python scripts/generate_qr.py https://memories.wrightideas.biz/tribute/abc123
  python scripts/generate_qr.py https://memories.wrightideas.biz/tribute/abc123 my-stone
"""
import sys
import qrcode
import qrcode.image.svg
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
from pathlib import Path

def make_qr(url: str, name: str) -> None:
    out_dir = Path(__file__).parent.parent / "qr_output"
    out_dir.mkdir(exist_ok=True)

    # SVG — for stone engraving
    qr = qrcode.QRCode(
        version=3,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    svg_path = out_dir / f"{name}.svg"
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    with open(svg_path, "wb") as f:
        img.save(f)
    print(f"SVG: {svg_path}")

    # PNG — rounded style, for reference
    qr2 = qrcode.QRCode(
        version=3,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr2.add_data(url)
    qr2.make(fit=True)
    png_path = out_dir / f"{name}.png"
    img2 = qr2.make_image(image_factory=StyledPilImage, module_drawer=RoundedModuleDrawer())
    img2.save(png_path)
    print(f"PNG: {png_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "qr-code"
    make_qr(url, name)
