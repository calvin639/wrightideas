"""
SQS trigger: Montage Builder

Downloads all generated video clips for an order, stitches them into a
final memorial video using FFmpeg, adds a title card and background music,
uploads the result to S3, generates a QR code, and emails the customer.

Requires FFmpeg to be available on $PATH (via Lambda layer).
See README for instructions on adding the FFmpeg layer.

SQS message:
{
  "order_id": "uuid",
  "partial": false    // true if some clips failed
}
"""

import json
import os
import subprocess
import logging
import tempfile
from pathlib import Path

import boto3
import requests

from shared.db import get_order, get_order_files, update_order_status
from shared.models import OrderStatus, FileStatus, now_iso
from shared.qr_utils import generate_and_upload_qr
from shared.email_utils import send_video_ready

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://memories.wrightideas.co")

s3 = boto3.client("s3")


def lambda_handler(event, context):
    results = {"batchItemFailures": []}

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            body = json.loads(record["body"])
            _build_montage(body["order_id"])
        except Exception as e:
            logger.error(f"Montage failed for record {message_id}: {e}", exc_info=True)
            results["batchItemFailures"].append({"itemIdentifier": message_id})

    return results


def _build_montage(order_id: str) -> None:
    """Full montage pipeline for one order."""
    logger.info(f"Building montage for order {order_id}")

    order = get_order(order_id)
    if not order:
        raise ValueError(f"Order {order_id} not found")

    files = get_order_files(order_id)
    done_files = sorted(
        [f for f in files if f.status == FileStatus.DONE],
        key=lambda f: f.sort_order,
    )

    if not done_files:
        raise ValueError(f"No completed clips for order {order_id}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        clip_paths = []

        # ── 1. Download clips from S3 ─────────────────────────────────────────
        logger.info(f"Downloading {len(done_files)} clips…")
        for i, f in enumerate(done_files):
            local_path = tmp / f"clip_{i:02d}.mp4"
            s3.download_file(VIDEOS_BUCKET, f.generated_video_s3_key, str(local_path))
            clip_paths.append(local_path)
            logger.info(f"  Downloaded clip {i+1}/{len(done_files)}")

        # ── 2. Create title card ──────────────────────────────────────────────
        title_clip = tmp / "title.mp4"
        _create_title_card(
            output_path=str(title_clip),
            loved_one_name=order.loved_one_name,
            dob=order.loved_one_dob,
            dod=order.loved_one_dod,
        )
        all_clips = [title_clip] + clip_paths

        # ── 3. Normalise clips to consistent format ───────────────────────────
        logger.info("Normalising clips…")
        normalised = []
        for i, clip in enumerate(all_clips):
            out = tmp / f"norm_{i:02d}.mp4"
            _normalise_clip(str(clip), str(out))
            normalised.append(out)

        # ── 4. Concatenate ────────────────────────────────────────────────────
        logger.info("Concatenating clips…")
        concat_file = tmp / "concat.txt"
        with open(concat_file, "w") as cf:
            for clip in normalised:
                cf.write(f"file '{clip}'\n")

        raw_output = tmp / "montage_raw.mp4"
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy",
            str(raw_output),
        ])

        # ── 5. Add background music + fade in/out ─────────────────────────────
        logger.info("Adding music…")
        music_path = _get_background_music(tmp)
        final_output = tmp / "montage_final.mp4"

        if music_path and music_path.exists():
            _run_ffmpeg([
                "-i", str(raw_output),
                "-i", str(music_path),
                "-filter_complex",
                "[1:a]volume=0.3,afade=t=in:st=0:d=2,afade=t=out:st=max(0\\,duration-3):d=3[music];"
                "[0:a]volume=0.0[silent];"   # no original audio from clips
                "[music]anull[a]",
                "-map", "0:v",
                "-map", "[a]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                str(final_output),
            ])
        else:
            # No music — just copy
            logger.warning("No background music available, skipping audio")
            final_output = raw_output

        # ── 6. Upload final video to S3 ───────────────────────────────────────
        logger.info("Uploading final video…")
        video_key = f"tributes/{order_id}/memorial.mp4"
        s3.upload_file(
            str(final_output),
            VIDEOS_BUCKET,
            video_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        video_url = f"https://{VIDEOS_BUCKET}.s3.eu-west-1.amazonaws.com/{video_key}"
        logger.info(f"Video uploaded: {video_url}")

        # ── 7. Generate QR code ───────────────────────────────────────────────
        logger.info("Generating QR code…")
        tribute_page_url = f"{FRONTEND_URL}/tribute/{order_id}"
        qr_key, qr_url = generate_and_upload_qr(order_id)

        # ── 8. Mark order complete ─────────────────────────────────────────────
        update_order_status(
            order_id,
            OrderStatus.COMPLETE,
            video_url=video_url,
            video_s3_key=video_key,
            qr_code_url=qr_url,
            qr_code_s3_key=qr_key,
            tribute_page_url=tribute_page_url,
            completed_at=now_iso(),
        )
        logger.info(f"Order {order_id} marked COMPLETE")

        # ── 9. Send customer email ─────────────────────────────────────────────
        order = get_order(order_id)  # re-fetch with updated fields
        if order:
            send_video_ready(order)
            logger.info(f"Completion email sent to {order.customer_email}")


def _create_title_card(
    output_path: str,
    loved_one_name: str,
    dob: str = "",
    dod: str = "",
    duration: int = 5,
) -> None:
    """
    Create a title card video using Pillow for text rendering + FFmpeg for video.

    We use Pillow instead of FFmpeg's drawtext filter because the bundled
    imageio-ffmpeg binary is a minimal build without libfreetype support.
    """
    from PIL import Image, ImageDraw, ImageFont
    import tempfile, os

    W, H = 1280, 720
    GOLD = (200, 168, 130)
    WHITE = (255, 255, 255)
    BG = (0, 0, 0)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Use default Pillow font (always available — no external font file needed)
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except OSError:
        # Lambda may not have DejaVu — fall back to PIL default bitmap font
        font_large = ImageFont.load_default()
        font_med   = font_large
        font_small = font_large

    dates_line = ""
    if dob and dod:
        dates_line = f"{_format_date(dob)} — {_format_date(dod)}"
    elif dod:
        dates_line = _format_date(dod)

    def centred_text(draw, text, font, y, color):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((W - w) / 2, y), text, font=font, fill=color)

    if dates_line:
        centred_text(draw, "In Loving Memory of", font_med,   H // 2 - 90, GOLD)
        centred_text(draw, loved_one_name,         font_large, H // 2 - 30, WHITE)
        centred_text(draw, dates_line,             font_small, H // 2 + 50, GOLD)
    else:
        centred_text(draw, "In Loving Memory of", font_med,   H // 2 - 60, GOLD)
        centred_text(draw, loved_one_name,         font_large, H // 2 + 10, WHITE)

    # Save PNG to temp file, then convert to video with FFmpeg
    png_path = output_path.replace(".mp4", "_title.png")
    img.save(png_path)

    _run_ffmpeg([
        "-loop", "1",
        "-i", png_path,
        "-vf", f"fade=t=in:st=0:d=1,fade=t=out:st={duration - 1}:d=1",
        "-t", str(duration),
        "-r", "24",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        output_path,
    ])

    try:
        os.remove(png_path)
    except OSError:
        pass


def _normalise_clip(input_path: str, output_path: str) -> None:
    """Normalise a clip to consistent resolution, framerate, and codec."""
    _run_ffmpeg([
        "-i", input_path,
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
               "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,"
               "fps=24",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        "-pix_fmt", "yuv420p",
        output_path,
    ])


def _get_background_music(tmp: Path) -> Path | None:
    """
    Fetch background music for the montage.
    Falls back to a bundled default track (include a gentle_music.mp3 in src/).
    Future: integrate Artlist Enterprise API to pull licensed tracks.
    """
    # Check for a bundled default track
    bundled = Path(__file__).parent / "assets" / "gentle_music.mp3"
    if bundled.exists():
        return bundled

    # TODO: Add Artlist Enterprise API integration here to pull a licensed track
    # Example:
    # resp = requests.get(
    #     "https://api.artlist.io/v1/songs",
    #     headers={"Authorization": f"Bearer {ARTLIST_API_KEY}"},
    #     params={"mood": "emotional", "limit": 1}
    # )
    # track_url = resp.json()["songs"][0]["downloadUrl"]
    # local_path = tmp / "music.mp3"
    # local_path.write_bytes(requests.get(track_url).content)
    # return local_path

    logger.warning("No background music found. Add gentle_music.mp3 to assets/")
    return None


def _get_ffmpeg() -> str:
    """Return path to FFmpeg binary — bundled via imageio-ffmpeg in Lambda layer."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # fallback for local dev


def _run_ffmpeg(args: list) -> None:
    """Run an FFmpeg command, raising on failure."""
    cmd = [_get_ffmpeg(), "-y", "-loglevel", "error"] + args
    logger.debug(f"FFmpeg: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr}")


def _format_date(iso_date: str) -> str:
    """Convert ISO date to human-readable format: '12 March 1945'"""
    try:
        from datetime import datetime
        dt = datetime.strptime(iso_date[:10], "%Y-%m-%d")
        return dt.strftime("%-d %B %Y")
    except Exception:
        return iso_date
