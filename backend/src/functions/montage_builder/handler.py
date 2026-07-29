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
from shared.models import (
    OrderStatus, FileStatus, MediaKind, MAX_VIDEO_SECONDS, now_iso,
)
from shared.qr_utils import generate_and_upload_qr
from shared.email_utils import send_video_ready

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET", "")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
VIDEOS_CF_URL = os.environ.get("VIDEOS_CF_URL", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://memories.wrightideas.co")
MUSIC_KEY_PREFIX = os.environ.get("MUSIC_KEY_PREFIX", "music/")

s3 = boto3.client("s3")


def lambda_handler(event, context):
    """Invoked directly by the state machine as the final step.

    Input: {"order_id": "..."}. Raising here fails the execution, which is the
    correct behaviour — by this point the customer has paid and there is no
    partial result worth delivering.
    """
    order_id = event["order_id"]
    _build_montage(order_id)
    return {"order_id": order_id, "status": OrderStatus.COMPLETE.value}


def _build_montage(order_id: str) -> None:
    """Full montage pipeline for one order."""
    logger.info(f"Building montage for order {order_id}")

    order = get_order(order_id)
    if not order:
        raise ValueError(f"Order {order_id} not found")

    files = get_order_files(order_id)

    # Two kinds of usable material:
    #   DONE    — a Runway clip generated from a photo
    #   SKIPPED — a customer-uploaded video, which never went to Runway
    # Both are ordered together by sort_order so the montage follows the
    # sequence the customer chose, regardless of how each item was produced.
    usable = sorted(
        [
            f for f in files
            if (f.status == FileStatus.DONE and f.generated_video_s3_key)
            or (f.status == FileStatus.SKIPPED and f.media_kind == MediaKind.VIDEO)
        ],
        key=lambda f: f.sort_order,
    )

    if not usable:
        raise ValueError(f"No usable clips for order {order_id}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        clip_paths = []

        # ── 1. Collect source clips ───────────────────────────────────────────
        logger.info("Collecting %s clips…", len(usable))
        for i, f in enumerate(usable):
            local_path = tmp / f"clip_{i:02d}.mp4"
            if f.media_kind == MediaKind.VIDEO:
                _fetch_customer_video(f, local_path)
            else:
                s3.download_file(VIDEOS_BUCKET, f.generated_video_s3_key, str(local_path))
            clip_paths.append(local_path)
            logger.info("  Clip %s/%s ready", i + 1, len(usable))

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
        music_path = _get_background_music(tmp, music_choice=order.music_choice)
        final_output = tmp / "montage_final.mp4"

        if music_path and music_path.exists():
            _run_ffmpeg([
                "-i", str(raw_output),
                "-i", str(music_path),
                "-filter_complex",
                # The clips' own audio is already silent (see _normalise_clip),
                # so the music is simply mapped over the top. The previous
                # version declared a [silent] label here and never used it,
                # which FFmpeg treats as an unconnected output pad.
                "[1:a]volume=0.3,afade=t=in:st=0:d=2[a]",
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
        # The videos bucket has all public access blocked and only allows reads
        # via the CloudFront distribution (OAC). Always serve through CloudFront —
        # a direct S3 URL will return 403.
        if VIDEOS_CF_URL:
            video_url = f"{VIDEOS_CF_URL.rstrip('/')}/{video_key}"
        else:
            # Fallback only for local/dev runs without the CF distribution wired up.
            video_url = f"https://{VIDEOS_BUCKET}.s3.eu-west-1.amazonaws.com/{video_key}"
            logger.warning("VIDEOS_CF_URL not set — falling back to direct S3 URL (will 403 in prod)")
        logger.info(f"Video uploaded: {video_url}")

        # ── 7. Generate QR code ───────────────────────────────────────────────
        logger.info("Generating QR code…")
        tribute_page_url = f"{FRONTEND_URL}/tribute/{order_id}"
        qr_key, qr_url, qr_svg_key, qr_svg_url = generate_and_upload_qr(order_id)

        # ── 8. Mark order complete ─────────────────────────────────────────────
        update_order_status(
            order_id,
            OrderStatus.COMPLETE,
            video_url=video_url,
            video_s3_key=video_key,
            qr_code_url=qr_url,
            qr_code_s3_key=qr_key,
            qr_svg_url=qr_svg_url,
            qr_svg_s3_key=qr_svg_key,
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

    # Use default Pillow font (always available — no external font file needed).
    # Sizes are 3x the original (was 52/36/28) so the title card is readable
    # at a glance — especially on mobile when viewing the tribute page.
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 156)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 108)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 84)
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

    # y-offsets from H/2 also scaled 3x (was -90/-30/+50 and -60/+10) so the
    # taller font sizes don't overlap.
    if dates_line:
        centred_text(draw, "In Loving Memory of", font_med,   H // 2 - 270, GOLD)
        centred_text(draw, loved_one_name,         font_large, H // 2 - 90,  WHITE)
        centred_text(draw, dates_line,             font_small, H // 2 + 150, GOLD)
    else:
        centred_text(draw, "In Loving Memory of", font_med,   H // 2 - 180, GOLD)
        centred_text(draw, loved_one_name,         font_large, H // 2 + 30,  WHITE)

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


def _fetch_customer_video(f, dest: Path) -> None:
    """Download a customer-uploaded video and trim it to the allowed window.

    Trimming happens here rather than in the browser: the montage Lambda already
    carries FFmpeg, whereas client-side trimming would mean shipping ffmpeg.wasm
    (~30MB) to a grieving customer on a phone. The customer picks the start
    point in the UI; this cuts MAX_VIDEO_SECONDS from there.

    `-ss` before `-i` seeks the input, which is fast but snaps to a keyframe.
    That is the right trade for a home video — a fraction of a second either way
    is invisible, and accurate seeking would mean decoding the whole file.
    """
    raw = dest.with_name(dest.stem + "_raw.mp4")
    s3.download_file(UPLOADS_BUCKET, f.s3_key, str(raw))

    start = max(0.0, float(f.trim_start_seconds or 0.0))
    logger.info(
        "Trimming customer video %s from %.1fs for %.1fs",
        f.file_id, start, MAX_VIDEO_SECONDS,
    )
    _run_ffmpeg([
        "-ss", f"{start:.2f}",
        "-i", str(raw),
        "-t", f"{MAX_VIDEO_SECONDS:.2f}",
        # Re-encoded rather than stream-copied: a copy would start at the
        # nearest keyframe and can produce a corrupt leading segment on concat.
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        # Audio is dropped deliberately — the montage plays one continuous
        # music bed, and cutting between clip audio and music is jarring.
        "-an",
        str(dest),
    ])
    raw.unlink(missing_ok=True)


def _normalise_clip(input_path: str, output_path: str) -> None:
    """Normalise a clip to consistent resolution, framerate, and codec.

    Portrait sources are letterboxed over a blurred copy of themselves rather
    than black bars — the same treatment `image_prep` gives portrait photos, so
    generated and uploaded material look like one piece. Customer videos are the
    only way a non-16:9 clip reaches this point, since prepared photos are
    already 1280x720.

    An explicit silent audio track is generated for every clip. Without it,
    concatenating clips that have audio with clips that do not produces
    stream-count mismatches and silent dropouts.
    """
    _run_ffmpeg([
        "-i", input_path,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex",
        "[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
        "crop=1280:720,boxblur=20:2,eq=brightness=-0.25[bg];"
        "[0:v]scale=1280:720:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=24[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        "-pix_fmt", "yuv420p",
        "-shortest",
        output_path,
    ])


def _get_background_music(tmp: Path, music_choice: str = "") -> Path | None:
    """Download the chosen music track from S3 (music/{choice}.mp3 in VIDEOS_BUCKET)."""
    if not music_choice or music_choice == "none":
        return None

    local_path = tmp / "music.mp3"
    s3_key = f"{MUSIC_KEY_PREFIX}{music_choice}.mp3"
    try:
        s3.download_file(VIDEOS_BUCKET, s3_key, str(local_path))
        logger.info(f"Downloaded music: {s3_key}")
        return local_path
    except Exception as e:
        logger.warning(f"Could not download music '{s3_key}': {e} — skipping audio")
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
