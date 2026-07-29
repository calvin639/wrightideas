"""
Generated-clip storage.

Previously this logic existed byte-for-byte in both `runway_webhook` and
`runway_poller`, which is how the two ended up drifting apart — the poller
gained a status guard that the webhook never got, and the pair could both decide
an order was finished and enqueue two montages. There is now one copy, and
Step Functions owns the completion decision.
"""

import logging
import os

import boto3
import requests

logger = logging.getLogger(__name__)

VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET", "")
DOWNLOAD_TIMEOUT = 180

s3 = boto3.client("s3")


def store_clip(runway_url: str, order_id: str, file_id: str) -> str:
    """Download a finished Runway clip into our own S3 and return its key.

    Runway's output URLs expire, so the clip has to be copied before the montage
    stage runs — which may be minutes later if other clips are still generating.
    """
    resp = requests.get(runway_url, timeout=DOWNLOAD_TIMEOUT)
    resp.raise_for_status()

    s3_key = f"clips/{order_id}/{file_id}.mp4"
    s3.put_object(
        Bucket=VIDEOS_BUCKET,
        Key=s3_key,
        Body=resp.content,
        ContentType="video/mp4",
    )
    logger.info("Stored clip %s (%s bytes)", s3_key, len(resp.content))
    return s3_key
