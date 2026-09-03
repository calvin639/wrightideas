"""
Montage Builder — the final step of the state machine.

Collects every usable clip for an order, renders an intro, cross-dissolves the
clips into a body, rewinds that body as an outro, lays the chosen music over the
whole thing, uploads it, generates the QR code and emails the customer.

Shape of the finished film:

    intro  ──▶  body (all clips, cross-dissolved)  ──▶  rewind  ──▶  end card
      │                                                    │           │
      └─ name on black, then the photos assemble            │           └─ name + dates
         into a wall of prints                              └─ the montage at
                                                               speed, landing
                                                               back on the wall

The intro style comes from the order (`wall` or `flipbook`); both end on the
same wall, which is why one outro serves both. See sequence.py.

Requires FFmpeg on $PATH (via the dependencies layer's imageio-ffmpeg binary).

Invoked directly by the state machine: {"order_id": "uuid"}
"""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import boto3

from shared.db import get_order, get_order_files, update_order_status
from shared.models import (
    OrderStatus, FileStatus, MediaKind, MAX_VIDEO_SECONDS, now_iso,
)
from shared.qr_utils import generate_and_upload_qr
from shared.email_utils import send_video_ready

from . import sequence
from .sequence import INTRO_STYLES, WALL_INTRO, MAX_WALL, W, H, FPS

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET", "")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
VIDEOS_CF_URL = os.environ.get("VIDEOS_CF_URL", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://memories.wrightideas.co")
MUSIC_KEY_PREFIX = os.environ.get("MUSIC_KEY_PREFIX", "music/")

# Percentage of an order's files that must have produced usable content before
# a montage is worth delivering. A tribute missing most of its photos is worse
# than a delayed one — below this we fail loudly so the admin alert fires.
MIN_SUCCESS_PCT = float(os.environ.get("MONTAGE_MIN_SUCCESS_PCT", "80"))

# Cross-dissolve between clips. Long enough to feel like a memory giving way to
# the next, short enough that a 4-second clip is still mostly itself.
XFADE = float(os.environ.get("MONTAGE_XFADE_SECONDS", "0.8"))
# The rewind is held to roughly this length whatever the montage runs to, by
# dropping frames before reversing rather than after.
SCRUB_TARGET = float(os.environ.get("MONTAGE_SCRUB_SECONDS", "6.0"))

s3 = boto3.client("s3")


def lambda_handler(event, context):
    """Input: {"order_id": "..."}.

    Raising here fails the execution, which is the correct behaviour — by this
    point the customer has paid and there is no partial result worth delivering.
    """
    order_id = event["order_id"]
    _build_montage(order_id)
    return {"order_id": order_id, "status": OrderStatus.COMPLETE.value}


def _build_montage(order_id: str) -> None:
    logger.info("Building montage for order %s", order_id)

    order = get_order(order_id)
    if not order:
        raise ValueError(f"Order {order_id} not found")

    files = get_order_files(order_id)

    # Two kinds of usable material:
    #   DONE    — a clip generated from a photo
    #   SKIPPED — a customer-uploaded video, which never went to the generator
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

    # Completion threshold. This is the REAL gate: the Map states'
    # ToleratedFailurePercentage never fires, because every failing iteration is
    # caught and converted to a Pass, so the Map always reports success. Without
    # the check below a 1-of-5 order would ship as a "finished" tribute.
    expected = [f for f in files
                if f.media_kind != MediaKind.IMAGE or f.status != FileStatus.SKIPPED]
    total = len(expected) or len(files)
    usable_ids = {f.file_id for f in usable}
    pct = (len(usable) / total * 100) if total else 0
    if pct < MIN_SUCCESS_PCT:
        failed = [
            f"{f.original_filename or f.file_id}: {f.error_message or f.status}"
            for f in files if f.file_id not in usable_ids
        ]
        raise ValueError(
            f"Only {len(usable)}/{total} files usable ({pct:.0f}%), "
            f"below the {MIN_SUCCESS_PCT:.0f}% threshold for order {order_id}. "
            f"Failures — {'; '.join(failed) or 'none recorded'}"
        )
    if len(usable) < total:
        logger.warning("Order %s building with %s/%s files (%.0f%%) — %s dropped",
                       order_id, len(usable), total, pct, total - len(usable))

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # ── 1. Collect and normalise the clips ────────────────────────────────
        logger.info("Collecting %s clips…", len(usable))
        normalised = []
        for i, f in enumerate(usable):
            raw = tmp / f"clip_{i:02d}.mp4"
            if f.media_kind == MediaKind.VIDEO:
                _fetch_customer_video(f, raw)
            else:
                s3.download_file(VIDEOS_BUCKET, f.generated_video_s3_key, str(raw))
            norm = tmp / f"norm_{i:02d}.mp4"
            _normalise_clip(str(raw), str(norm))
            raw.unlink(missing_ok=True)          # /tmp is finite; 30 clips is not
            normalised.append(norm)
            logger.info("  Clip %s/%s ready", i + 1, len(usable))

        # ── 2. Wall material ──────────────────────────────────────────────────
        # Every wall tile is the clip's OWN first frame. For generated clips that
        # frame is the prepared photo (it is submitted as both first and last
        # keyframe), so the tile and the cut-out register to the pixel; for a
        # customer video it is a sensible still. Either way the wall can never
        # disagree with the montage about how a photo was framed.
        items = _wall_items(order_id, usable, normalised, tmp)

        # ── 3. Intro ──────────────────────────────────────────────────────────
        style = (order.intro_style or WALL_INTRO).strip().lower()
        if style not in INTRO_STYLES:
            logger.warning("Unknown intro_style %r on order %s — using %s",
                           style, order_id, WALL_INTRO)
            style = WALL_INTRO
        logger.info("Rendering '%s' intro over %s items…", style, len(items))
        intro = tmp / "intro.mp4"
        _encode_frames(
            sequence.render_intro(items, order.loved_one_name,
                                  message=order.stone_message, style=style),
            str(intro),
        )

        # ── 4. Body ───────────────────────────────────────────────────────────
        logger.info("Cross-dissolving %s clips…", len(normalised))
        body = tmp / "body.mp4"
        _concat_with_transitions([str(p) for p in normalised], str(body))

        # ── 5. Outro: the montage rewound, landing back on the wall ───────────
        logger.info("Building outro…")
        scrub = tmp / "scrub.mp4"
        _rewind(str(body), str(scrub))
        tail = tmp / "tail.mp4"
        _encode_frames(
            sequence.render_outro_tail(items, order.loved_one_name,
                                       dates=_dates_line(order)),
            str(tail),
        )

        # ── 6. Join and score ─────────────────────────────────────────────────
        silent = tmp / "silent.mp4"
        _join([str(intro), str(body), str(scrub), str(tail)], str(silent),
              fades=[0.9, 0.5, 0.7])

        final_output = tmp / "montage_final.mp4"
        music_path = _get_background_music(tmp, music_choice=order.music_choice)
        _add_music(str(silent), music_path, str(final_output))

        # A montage is not finished until it decodes. An ffmpeg process killed
        # partway through leaves a file of entirely plausible size whose moov
        # atom was never written — it uploads fine and plays nowhere.
        _verify_playable(str(final_output))

        # ── 7. Upload ─────────────────────────────────────────────────────────
        logger.info("Uploading final video…")
        video_key = f"tributes/{order_id}/memorial.mp4"
        s3.upload_file(str(final_output), VIDEOS_BUCKET, video_key,
                       ExtraArgs={"ContentType": "video/mp4"})
        # The videos bucket has all public access blocked and only allows reads
        # via the CloudFront distribution (OAC). Always serve through CloudFront —
        # a direct S3 URL will return 403.
        if VIDEOS_CF_URL:
            video_url = f"{VIDEOS_CF_URL.rstrip('/')}/{video_key}"
        else:
            video_url = f"https://{VIDEOS_BUCKET}.s3.eu-west-1.amazonaws.com/{video_key}"
            logger.warning("VIDEOS_CF_URL not set — falling back to direct S3 URL "
                           "(will 403 in prod)")
        logger.info("Video uploaded: %s", video_url)

        # ── 8. QR code, order record, email ───────────────────────────────────
        tribute_page_url = f"{FRONTEND_URL}/tribute/{order_id}"
        qr_key, qr_url, qr_svg_key, qr_svg_url = generate_and_upload_qr(order_id)

        update_order_status(
            order_id, OrderStatus.COMPLETE,
            video_url=video_url, video_s3_key=video_key,
            qr_code_url=qr_url, qr_code_s3_key=qr_key,
            qr_svg_url=qr_svg_url, qr_svg_s3_key=qr_svg_key,
            tribute_page_url=tribute_page_url, completed_at=now_iso(),
        )
        logger.info("Order %s marked COMPLETE", order_id)

        order = get_order(order_id)   # re-fetch with updated fields
        if order:
            send_video_ready(order)
            logger.info("Completion email sent to %s", order.customer_email)


# ── wall material ─────────────────────────────────────────────────────────────

def _wall_items(order_id: str, files: list, clips: list, tmp: Path) -> list:
    """Build the wall: a tile per clip, a cut-out where one exists.

    The wall holds MAX_WALL prints legibly. Beyond that we keep a spread across
    the running order rather than the first twelve, so the wall still reads as a
    whole life and not just its opening chapter.
    """
    from PIL import Image

    n = len(clips)
    if n > MAX_WALL:
        idx = sorted({round(i * (n - 1) / (MAX_WALL - 1)) for i in range(MAX_WALL)})
    else:
        idx = list(range(n))

    items = []
    for i, (f, clip) in enumerate(zip(files, clips)):
        frame_png = tmp / f"tile_{i:02d}.png"
        _run_ffmpeg(["-i", str(clip), "-frames:v", "1", str(frame_png)])
        item = {
            "key": f.file_id,
            "tile": Image.open(frame_png).convert("RGB"),
            "on_wall": i in idx,
        }
        cut = _fetch_cutout(order_id, f, tmp)
        if cut is not None:
            item["cutout"] = cut
        items.append(item)

    # Only wall items get a home on the wall; the flipbook riffles the whole
    # album past the lens, so every item still needs a card of its own.
    sequence.build_layout([it for it in items if it["on_wall"]])
    for it in items:
        it.setdefault("card", sequence.framed(it["tile"]))
        it.setdefault("card_scale", it["card"].width / W * 0.445)
    return items


def _fetch_cutout(order_id: str, f, tmp: Path):
    """The subject cut free of its background, if image_prep produced one.

    Optional by design: cut-outs need a segmentation model that only exists in
    the image_prep container, and a photo without one simply flies in as a print
    instead of stepping out of the dark.
    """
    if f.media_kind != MediaKind.IMAGE or not f.prepared_s3_key:
        return None
    key = f.prepared_s3_key.rsplit(".", 1)[0] + "_cutout.png"
    dest = tmp / f"cut_{f.file_id}.png"
    try:
        s3.download_file(UPLOADS_BUCKET, key, str(dest))
    except Exception as e:                       # noqa: BLE001 — absence is normal
        logger.debug("No cut-out for %s (%s)", f.file_id, e)
        return None
    from PIL import Image
    return Image.open(dest).convert("RGBA")


# ── ffmpeg stages ─────────────────────────────────────────────────────────────

def _encode_frames(frames, output_path: str) -> None:
    """Pipe PIL frames straight into ffmpeg as raw video.

    Writing a few hundred JPEGs to /tmp and reading them back costs more time
    and more ephemeral storage than the encode itself.
    """
    cmd = [
        _get_ffmpeg(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    n = 0
    try:
        for frame in frames:
            proc.stdin.write(frame.tobytes())
            n += 1
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"FFmpeg failed encoding {n} frames:\n{err}")
    logger.info("  encoded %s frames -> %s", n, Path(output_path).name)


def _concat_with_transitions(clips: list, output_path: str) -> None:
    """Cross-dissolve every clip into the next in a single filter graph.

    Offsets accumulate on the running result, not on the individual clips: each
    xfade shortens the total by its own duration, so the next offset must be
    measured against what has been produced so far.
    """
    if len(clips) == 1:
        _run_ffmpeg(["-i", clips[0], "-c", "copy", output_path])
        return

    durations = [_duration(c) for c in clips]
    if not _has_filter("xfade") or min(durations) <= XFADE * 1.5:
        logger.warning("Falling back to hard cuts (xfade unavailable or clips too short)")
        return _concat_hard(clips, output_path)

    parts, prev, running = [], "0:v", durations[0]
    for i in range(1, len(clips)):
        label = f"x{i}" if i < len(clips) - 1 else "v"
        parts.append(f"[{prev}][{i}:v]xfade=transition=fade:"
                     f"duration={XFADE}:offset={running - XFADE:.3f}[{label}]")
        running += durations[i] - XFADE
        prev = label
    args = []
    for c in clips:
        args += ["-i", c]
    _run_ffmpeg(args + ["-filter_complex", ";".join(parts), "-map", "[v]", "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", output_path])


def _concat_hard(clips: list, output_path: str) -> None:
    listing = Path(output_path).with_suffix(".txt")
    listing.write_text("".join(f"file '{c}'\n" for c in clips))
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", output_path])


def _rewind(body_path: str, output_path: str) -> None:
    """The montage at speed, backwards.

    Frames are DROPPED BEFORE `reverse`, not after. `reverse` buffers every
    frame it is handed, so reversing a two-minute 720p body directly would need
    gigabytes; subsampling first ties peak memory to the length of the rewind
    instead. `tmix` smears the survivors back into motion so it reads as a rush
    rather than a slideshow.
    """
    step = max(2, round(_duration(body_path) / max(1.0, SCRUB_TARGET)))
    vf = (f"select='not(mod(n,{step}))',setpts=N/{FPS}/TB,reverse,"
          f"tmix=frames=3:weights='1 1 1',fps={FPS}")
    if not _has_filter("tmix"):
        vf = f"select='not(mod(n,{step}))',setpts=N/{FPS}/TB,reverse,fps={FPS}"
    _run_ffmpeg(["-i", body_path, "-vf", vf, "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", output_path])
    logger.info("  rewind: every %sth frame -> %.1fs", step, _duration(output_path))


def _join(parts: list, output_path: str, fades: list) -> None:
    """Dissolve the four sections together."""
    durs = [_duration(p) for p in parts]
    if not _has_filter("xfade"):
        return _concat_hard(parts, output_path)
    graph, prev, running = [], "0:v", durs[0]
    for i in range(1, len(parts)):
        d = fades[i - 1]
        label = f"j{i}" if i < len(parts) - 1 else "v"
        graph.append(f"[{prev}][{i}:v]xfade=transition=fade:"
                     f"duration={d}:offset={running - d:.3f}[{label}]")
        running += durs[i] - d
        prev = label
    args = []
    for p in parts:
        args += ["-i", p]
    _run_ffmpeg(args + ["-filter_complex", ";".join(graph), "-map", "[v]", "-an",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                        "-pix_fmt", "yuv420p", output_path])


def _add_music(video_path: str, music_path, output_path: str) -> None:
    """Lay the music bed over the finished picture.

    `-shortest` is deliberately NOT used here. It ends the output at the end of
    the *shortest* stream, so a montage longer than its music track would be
    silently truncated — a 92-second track would cut a three-minute tribute off
    mid-clip. The audio is looped to cover the picture and trimmed to it
    instead, and `+faststart` moves the index to the front so the tribute page
    can start playing before the whole file has downloaded.
    """
    duration = _duration(video_path)
    if not music_path or not Path(music_path).exists():
        logger.warning("No background music available — muxing silent")
        _run_ffmpeg(["-i", video_path, "-c:v", "copy", "-movflags", "+faststart",
                     output_path])
        return
    fade_out_at = max(0.0, duration - 4.5)
    _run_ffmpeg([
        "-i", video_path,
        # -stream_loop repeats the track as many times as the picture needs.
        "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex",
        f"[1:a]volume=0.30,afade=t=in:st=0:d=3,"
        f"afade=t=out:st={fade_out_at:.2f}:d=4.5[a]",
        "-map", "0:v", "-map", "[a]",
        "-t", f"{duration:.3f}",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ])


def _verify_playable(path: str) -> None:
    """Decode the whole file. Cheap next to the encode, and the only check that
    catches a truncated container."""
    proc = subprocess.run([_get_ffmpeg(), "-v", "error", "-i", path, "-f", "null", "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0 or proc.stderr.strip():
        raise RuntimeError(f"Montage failed verification: {proc.stderr.strip()[:2000]}")
    logger.info("Montage verified: %.1fs, %s bytes", _duration(path), os.path.getsize(path))


# ── clip preparation ──────────────────────────────────────────────────────────

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
    logger.info("Trimming customer video %s from %.1fs for %.1fs",
                f.file_id, start, MAX_VIDEO_SECONDS)
    _run_ffmpeg([
        "-ss", f"{start:.2f}", "-i", str(raw), "-t", f"{MAX_VIDEO_SECONDS:.2f}",
        # Re-encoded rather than stream-copied: a copy would start at the
        # nearest keyframe and can produce a corrupt leading segment on concat.
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        # Audio is dropped deliberately — the montage plays one continuous
        # music bed, and cutting between clip audio and music is jarring.
        "-an", str(dest),
    ])
    raw.unlink(missing_ok=True)


def _normalise_clip(input_path: str, output_path: str) -> None:
    """Normalise a clip to consistent resolution, framerate and codec.

    Non-16:9 sources are letterboxed over a blurred copy of themselves rather
    than black bars — the same treatment `image_prep` gives portrait photos, so
    generated and uploaded material look like one piece.

    Every clip comes out silent. The montage carries one music bed and no clip
    audio; xfade also refuses to work across streams with mismatched audio, so
    dropping it here keeps the filter graph in step 4 simple.
    """
    _run_ffmpeg([
        "-i", input_path,
        "-filter_complex",
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"boxblur=20:2,eq=brightness=-0.25[bg];"
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},setsar=1[v]",
        "-map", "[v]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", output_path,
    ])


def _get_background_music(tmp: Path, music_choice: str = ""):
    """Download the chosen track from S3 (music/{choice}.mp3 in VIDEOS_BUCKET)."""
    if not music_choice or music_choice == "none":
        return None
    local_path = tmp / "music.mp3"
    s3_key = f"{MUSIC_KEY_PREFIX}{music_choice}.mp3"
    try:
        s3.download_file(VIDEOS_BUCKET, s3_key, str(local_path))
        logger.info("Downloaded music: %s", s3_key)
        return local_path
    except Exception as e:                       # noqa: BLE001
        logger.warning("Could not download music '%s': %s — skipping audio", s3_key, e)
        return None


# ── ffmpeg plumbing ───────────────────────────────────────────────────────────

_FILTERS: set | None = None


def _has_filter(name: str) -> bool:
    """Whether the bundled ffmpeg build actually carries a filter.

    The layer ships imageio-ffmpeg's binary, which is a minimal build — it has
    no libfreetype, which is why title text is drawn with Pillow. Rather than
    assume which filters survived, ask once and degrade gracefully.
    """
    global _FILTERS
    if _FILTERS is None:
        try:
            out = subprocess.run([_get_ffmpeg(), "-hide_banner", "-filters"],
                                 capture_output=True, text=True, timeout=30).stdout
            # Tokenising the whole listing rather than picking a column: the
            # format of `-filters` has changed between builds, and mis-parsing
            # it would silently downgrade every montage to hard cuts.
            _FILTERS = set(out.split())
        except Exception:                        # noqa: BLE001
            _FILTERS = set()
        logger.info("ffmpeg filter listing: %s tokens", len(_FILTERS))
    return (name in _FILTERS) if _FILTERS else True


def _duration(path: str) -> float:
    """Duration in seconds.

    ffprobe is not in the layer — imageio-ffmpeg ships the ffmpeg binary only —
    so this reads ffmpeg's own header report. Reading the header rather than
    decoding matters: this is called once per clip, twice more per section and
    again for the music, and decoding a two-minute body four extra times is
    minutes of a 900-second budget spent on arithmetic.
    """
    proc = subprocess.run([_get_ffmpeg(), "-hide_banner", "-i", path],
                          capture_output=True, text=True)
    for line in proc.stderr.splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
            if stamp and stamp != "N/A":
                h, m, s = stamp.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    # Rawvideo pipes and some fragmented files carry no container duration;
    # fall back to decoding and taking the last reported timestamp.
    proc = subprocess.run([_get_ffmpeg(), "-i", path, "-f", "null", "-"],
                          capture_output=True, text=True)
    last = None
    for line in proc.stderr.splitlines():
        if "time=" in line:
            last = line.rsplit("time=", 1)[1].split(" ")[0].strip()
    if not last:
        raise RuntimeError(f"Could not determine duration of {path}:\n{proc.stderr[-800:]}")
    h, m, s = last.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _dates_line(order) -> str:
    if order.loved_one_dob and order.loved_one_dod:
        return f"{_format_date(order.loved_one_dob)}  —  {_format_date(order.loved_one_dod)}"
    if order.loved_one_dod:
        return _format_date(order.loved_one_dod)
    return ""


def _get_ffmpeg() -> str:
    """Path to the FFmpeg binary — bundled via imageio-ffmpeg in the layer."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                            # noqa: BLE001
        return "ffmpeg"                          # fallback for local dev


def _run_ffmpeg(args: list) -> None:
    cmd = [_get_ffmpeg(), "-y", "-loglevel", "error"] + args
    logger.debug("FFmpeg: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr}")


def _format_date(iso_date: str) -> str:
    """ISO date to '12 March 1945'."""
    try:
        from datetime import datetime
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%-d %B %Y")
    except Exception:                            # noqa: BLE001
        return iso_date
