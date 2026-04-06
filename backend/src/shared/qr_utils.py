"""
QR code generation for tribute page links.

Generates two formats:
- PNG  (rounded modules, styled)  — for online display and the tribute page
- SVG  (clean path, square modules) — for the stone maker to use in engraving
"""
import os
import io
import boto3
import qrcode
import qrcode.image.svg
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
import logging

logger = logging.getLogger(__name__)
s3 = boto3.client("s3")

VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://memories.wrightideas.biz")
VIDEOS_CF_URL = os.environ.get("VIDEOS_CF_URL", "")


def generate_and_upload_qr(order_id: str) -> tuple[str, str, str, str]:
    """
    Generate PNG and SVG QR codes for an order's tribute page and upload both to S3.
    Returns (png_s3_key, png_url, svg_s3_key, svg_url).
    """
    tribute_url = f"{FRONTEND_URL}/tribute/{order_id}"

    png_key, png_url = _generate_png(order_id, tribute_url)
    svg_key, svg_url = _generate_svg(order_id, tribute_url)

    return png_key, png_url, svg_key, svg_url


def _generate_png(order_id: str, tribute_url: str) -> tuple[str, str]:
    """Styled PNG — rounded modules, for online display."""
    qr = qrcode.QRCode(
        version=3,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(tribute_url)
    qr.make(fit=True)

    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=RoundedModuleDrawer(),
    )

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    s3_key = f"qr/{order_id}/qr-code.png"
    s3.put_object(
        Bucket=VIDEOS_BUCKET,
        Key=s3_key,
        Body=buffer.getvalue(),
        ContentType="image/png",
    )

    url = _public_url(s3_key)
    logger.info(f"QR PNG uploaded: {url}")
    return s3_key, url


def _generate_svg(order_id: str, tribute_url: str) -> tuple[str, str]:
    """Clean path SVG — square modules, for stone engraving."""
    qr = qrcode.QRCode(
        version=3,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(tribute_url)
    qr.make(fit=True)

    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)

    buffer = io.BytesIO()
    img.save(buffer)
    buffer.seek(0)

    s3_key = f"qr/{order_id}/qr-code.svg"
    s3.put_object(
        Bucket=VIDEOS_BUCKET,
        Key=s3_key,
        Body=buffer.getvalue(),
        ContentType="image/svg+xml",
    )

    url = _public_url(s3_key)
    logger.info(f"QR SVG uploaded: {url}")
    return s3_key, url


def _public_url(s3_key: str) -> str:
    if VIDEOS_CF_URL:
        return f"{VIDEOS_CF_URL.rstrip('/')}/{s3_key}"
    return f"https://{VIDEOS_BUCKET}.s3.eu-west-1.amazonaws.com/{s3_key}"
