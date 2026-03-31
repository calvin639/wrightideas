"""
QR code generation for tribute page links.
"""
import os
import io
import boto3
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
import logging

logger = logging.getLogger(__name__)
s3 = boto3.client("s3")

VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://memories.wrightideas.co")
VIDEOS_CF_URL = os.environ.get("VIDEOS_CF_URL", "")


def generate_and_upload_qr(order_id: str) -> tuple[str, str]:
    """
    Generate a QR code for an order's tribute page and upload it to S3.
    Returns (s3_key, public_url).
    """
    tribute_url = f"{FRONTEND_URL}/tribute/{order_id}"

    # Generate QR code with a clean style
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

    # Convert to bytes
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    # Upload to S3
    s3_key = f"qr/{order_id}/qr-code.png"
    s3.put_object(
        Bucket=VIDEOS_BUCKET,
        Key=s3_key,
        Body=buffer.getvalue(),
        ContentType="image/png",
    )

    # Use CloudFront URL if configured, otherwise fall back to direct S3 URL
    if VIDEOS_CF_URL:
        public_url = f"{VIDEOS_CF_URL.rstrip('/')}/{s3_key}"
    else:
        public_url = f"https://{VIDEOS_BUCKET}.s3.eu-west-1.amazonaws.com/{s3_key}"
    logger.info(f"QR code uploaded: {public_url}")
    return s3_key, public_url
