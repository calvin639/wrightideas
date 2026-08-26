"""
Deterministic image preparation for the Memories in Stone pipeline.

Runs between payment and Runway submission. Every uploaded photo is oriented,
framed to 16:9 landscape, tone-corrected, resized to 1280x720 and mildly
sharpened before Runway ever sees it. Feeding Runway a clean, correctly-framed
frame is what stops it inventing facial detail — which is what causes identity
drift across a 5-second clip.

DESIGN RULE: everything in this module is deterministic. No generative models,
no hallucinated detail, no AWS calls. That makes it unit-testable without mocks
and means it can never invent a feature on a real person's face.

Generative restoration (Real-ESRGAN / GFPGAN) is deliberately NOT here. It needs
torch, which needs a container image. `assess()` computes the tier that decides
whether a photo would benefit from it, so the data is being collected now and
the swap later is one function call — see `ImageAssessment.tier`.

Typical use:

    img = load_and_orient(raw_bytes)
    a   = assess(img)
    out = prepare(img, a, crop_rect=file.crop_rect)   # -> 1280x720 RGB
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, asdict, field, replace
from typing import Optional

import numpy as np
from PIL import Image, ImageOps, ImageFilter

logger = logging.getLogger(__name__)

# Pillow moved the resampling filters onto an enum in 9.1 and deprecated the
# module-level aliases. Resolve once so the module is warning-free on both.
try:
    RESAMPLE = Image.Resampling.LANCZOS
    RESAMPLE_FAST = Image.Resampling.BILINEAR
except AttributeError:  # Pillow < 9.1
    RESAMPLE = Image.LANCZOS
    RESAMPLE_FAST = Image.BILINEAR

# ── Output format ────────────────────────────────────────────────────────────

TARGET_W = 1280
TARGET_H = 720
TARGET_ASPECT = TARGET_W / TARGET_H          # 1.777…
JPEG_QUALITY = 92

# ── Tuning constants ─────────────────────────────────────────────────────────

# Sharpness is measured as variance-of-Laplacian on a fixed-size copy of the
# image. Measuring at native resolution makes the number scale with pixel count,
# so a big blurry scan can outscore a small crisp one. Normalising the
# measurement size makes the threshold meaningful across all inputs.
SHARPNESS_PROBE_EDGE = 1024

# Below this an image is treated as soft enough to hand to the face-restoration
# model. Set from real scans rather than theory: across a reference set of
# 1960s-70s prints, values ran 74-233 and every one of them visibly benefited
# from restoration, so the boundary sits above that range rather than through
# the middle of it. Sharp modern phone photos score far higher and are untouched.
# Tunable at deploy time — see SHARPNESS_SOFT in the function environment.
SHARPNESS_SOFT = float(os.environ.get("SHARPNESS_SOFT", "250"))

# Generative upscaling is only worth its cost when the shortfall is large.
# Real-ESRGAN is a 4x network — asking it for 1.15x still runs the full 4x
# convolution stack over the frame and then throws most of it away, which on a
# 3008MB Lambda is enough to be killed for running out of memory. Below this
# ratio Lanczos is indistinguishable and effectively free.
UPSCALE_TRIGGER = 1.5

# A source is "landscape enough" to crop to 16:9 without decapitating anyone.
# Below this we letterbox instead — cropping a portrait to 16:9 throws away 45%+
# of the frame height, which on a memorial photo means cutting off a head.
#
# Set just under 4:3 (1.333) deliberately. 4:3 is the dominant vintage print and
# slide format, it crops to 16:9 losing only the top and bottom 12.5%, and the
# result fills the frame far better than blurred bars. Near-square sources
# (~1.1 and below) still letterbox, because there the crop starts biting.
CROP_SAFE_ASPECT = 1.30

# Percentile stretch bounds. Deliberately conservative: clipping 0.5% at each
# end recovers contrast on faded scans without crushing highlights.
STRETCH_LO_PCT = 0.5
STRETCH_HI_PCT = 99.5
STRETCH_MIN_RANGE = 10      # skip the stretch if the channel is already flat

# Greyscale / sepia detection.
GREY_MAX_SPREAD = 8         # mean per-pixel (max channel - min channel)
SEPIA_MAX_SAT = 90          # mean HSV saturation, 0-255
SEPIA_HUE_LO = 8            # PIL hue is 0-255; ~11-50 deg covers red-orange-yellow
SEPIA_HUE_HI = 36

# Unsharp mask. Threshold is the important value — it stops flat, grainy areas
# (every vintage scan) from having their noise amplified into speckle.
UNSHARP_RADIUS = 1.2
UNSHARP_PERCENT_BASE = 60
UNSHARP_PERCENT_MAX = 150
UNSHARP_THRESHOLD = 3

# Letterbox background: the frame itself, blown up, blurred and darkened.
PAD_BLUR_RADIUS = 40
PAD_DARKEN = 0.45

# ── Upload guardrail thresholds ──────────────────────────────────────────────
# These drive advisory `warnings` on the assessment — they never block an
# order. Surfaced at upload time (help the customer retake the photo) and in
# the admin review email (flag files worth a closer look).

# Glare: a photo of a glossy print or a photo taken through glass has hard
# specular patches — near-white pixels with almost no texture inside them.
# Measured on the ERODED interior of the blown mask: the patch boundary has
# high gradients by definition, and including it inverts the test (a hard
# specular blob reads "textured", a flat white wall reads "flat").
#
# BOTH AREA BOUNDS ARE POST-EROSION FRACTIONS, not raw blown-pixel counts.
# _erode takes ~2px off every side of every patch, so a patch measures
# smaller here than it looks: a ~1% raw specular blob lands near 0.7% and
# does not fire. That is accepted looseness — many small highlights (a print
# under a lamp gives exactly that) can each erode below the bound and be
# missed. This is an advisory flag whose only cost of a miss is that nobody
# is prompted to rephotograph, so it is tuned to not cry wolf. Do NOT lower
# GLARE_AREA_WARN to "catch" those: the raw threshold it looks like is not
# the threshold it is, and dropping it re-admits the boundary-dominated
# false positives the erosion exists to remove.
GLARE_VALUE_MIN = 250          # 0-255 luminance for "blown out"
GLARE_FLAT_STD = 4.0           # a real bright surface still has texture
GLARE_AREA_WARN = 0.010        # post-erosion; >1% of the frame is a glare patch
GLARE_AREA_MAX = 0.20          # above this it's sky/overexposure, not glare

# Photo-of-a-photo / photo-of-a-screen: strong periodic texture (halftone
# printing, screen pixels, textured photo paper) shows as an isolated spectral
# peak in the mid-frequency band. The statistic is max/99.5th-percentile within
# a RADIAL frequency band — radial because rfft2 wraps negative frequencies to
# the far rows, and a naive "skip the first rows" mask silently includes
# low-frequency image content (a first version of this flagged every scan).
# Measured on a NATIVE-RESOLUTION central crop, never a resized probe: the
# periodic signal lives at 2-8 source pixels and resampling attenuates it —
# a second version of this had almost no margin for exactly that reason.
# Calibrated at native res on real 1960s-90s scans (2.5-5.2) vs synthetic
# screen-grid and halftone overlays incl. faint ones (13.2-75.6): threshold 9.
# Advisory only — borderline cases surface to a human, they don't block.
MOIRE_PEAK_RATIO = 9.0
MOIRE_BAND_LO = 0.12           # cycles/pixel at native scale
MOIRE_BAND_HI = 0.45

# A face-aware crop that cannot fit every face gets flagged for review.
WARN_LOW_RES_SCALE = 2.5       # upscaling beyond this can't fully hide


# ── Assessment ───────────────────────────────────────────────────────────────


@dataclass
class ImageAssessment:
    """Measured properties of a source photo, and what we plan to do with it."""

    width: int
    height: int
    aspect: float
    orientation: str          # "landscape" | "portrait" | "square"
    is_greyscale: bool
    is_sepia: bool
    sharpness: float
    frame_mode: str           # "crop" | "pad"
    scale_factor: float       # >1 means we are upscaling to reach 1280x720
    needs_upscale: bool       # too few pixels to fill the frame
    needs_face_restore: bool  # detail is soft — a face prior would recover it
    tier: int                 # 0 = none, 1 = upscale only, 2 = face restore
    reason: str
    # Advisory quality flags for the customer / reviewer. Never block an order.
    warnings: list = field(default_factory=list)
    # Face boxes transformed into the framed/prescaled image handed to the
    # restore callback — set transiently by prepare(), never persisted (see
    # to_dict). None = detection unavailable; [] = ran and found none.
    faces_framed: Optional[list] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("faces_framed", None)   # transient plumbing, not a measurement
        return d

    @property
    def preserves_tint(self) -> bool:
        """True when the tone pass must not touch per-channel colour balance.

        A per-channel percentile stretch neutralises a colour cast — which is
        exactly what you want on a colour photo and exactly what you must not do
        to a sepia print or a toned black-and-white, where the cast IS the image.
        """
        return self.is_greyscale or self.is_sepia


def load_and_orient(data: bytes) -> Image.Image:
    """Decode bytes and apply EXIF rotation, returning an RGB image.

    Phone photos are almost always stored unrotated with an EXIF orientation
    flag. Skipping this step yields sideways video.
    """
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def assess(img: Image.Image) -> ImageAssessment:
    """Measure a photo and decide how it should be framed and scaled."""
    w, h = img.size
    aspect = w / h

    if aspect > 1.05:
        orientation = "landscape"
    elif aspect < 0.95:
        orientation = "portrait"
    else:
        orientation = "square"

    is_grey, is_sepia = _detect_tone(img)
    sharpness = _sharpness(img)

    # Frame first, because the framing decision determines how much of the
    # source survives, and therefore how far we have to upscale.
    frame_mode = "crop" if aspect >= CROP_SAFE_ASPECT else "pad"
    scale_factor = _scale_factor(w, h, frame_mode)

    # These two needs are independent and drive different models.
    #
    #   needs_upscale      -> Real-ESRGAN. Only meaningful when the source has
    #                         fewer pixels than the output frame; upscaling an
    #                         image we are about to downscale achieves nothing.
    #   needs_face_restore -> GFPGAN. A large but soft scan has plenty of pixels
    #                         and still has mushy faces, which is the single
    #                         biggest driver of identity drift in the video.
    #
    # Most scanned prints land on face-restore-only: enough pixels, soft detail.
    needs_upscale = scale_factor > UPSCALE_TRIGGER
    needs_face_restore = sharpness < SHARPNESS_SOFT

    if needs_face_restore and needs_upscale:
        tier, reason = 2, "soft and under-resolution — face restore with upscaling"
    elif needs_face_restore:
        tier, reason = 2, "adequate resolution but soft — face restore"
    elif needs_upscale:
        tier, reason = 1, "sharp but under-resolution — upscale only"
    else:
        tier, reason = 0, "sufficient resolution and sharpness — no restoration"

    warnings = _quality_warnings(img, scale_factor)

    return ImageAssessment(
        width=w,
        height=h,
        aspect=round(aspect, 3),
        orientation=orientation,
        is_greyscale=is_grey,
        is_sepia=is_sepia,
        sharpness=round(sharpness, 1),
        frame_mode=frame_mode,
        scale_factor=round(scale_factor, 3),
        needs_upscale=needs_upscale,
        needs_face_restore=needs_face_restore,
        tier=tier,
        reason=reason,
        warnings=warnings,
    )


def _quality_warnings(img: Image.Image, scale_factor: float) -> list:
    """Advisory upload-quality flags — deterministic, cheap, never blocking.

    These exist to help the customer help themselves: most of what makes a
    memorial photo come out badly (photographing a glossy print under a lamp,
    photographing a screen, a tiny thumbnail saved from chat) is fixable at
    the source in thirty seconds — but only if someone says so at upload time.
    """
    warnings: list = []

    if scale_factor > WARN_LOW_RES_SCALE:
        warnings.append(
            "low_resolution: the photo is much smaller than the video frame — "
            "an original scan or full-quality export will look far better"
        )

    probe = _probe_copy(img, 512)
    arr = np.asarray(probe.convert("L"), dtype=np.float32)

    if _has_glare(arr):
        warnings.append(
            "possible_glare: bright reflective patches suggest a photo of a "
            "glossy print or through glass — re-photograph flat, near a window, "
            "with no direct light"
        )

    # The texture test reads a NATIVE-RESOLUTION central crop, not the resized
    # probe: halftone dots and screen pixels live at 2-8 source pixels, and a
    # thumbnail resample attenuates (or aliases) exactly those frequencies.
    w, h = img.size
    cw, ch = min(768, w), min(768, h)
    crop = img.crop(((w - cw) // 2, (h - ch) // 2,
                     (w - cw) // 2 + cw, (h - ch) // 2 + ch))
    native = np.asarray(crop.convert("L"), dtype=np.float32)

    if _has_periodic_texture(native):
        warnings.append(
            "possible_rephotographed_print_or_screen: strong regular texture "
            "suggests a photo of a printed or on-screen image — scan the "
            "original or use the original digital file if possible"
        )

    return warnings


def _erode(mask: np.ndarray, iterations: int = 2) -> np.ndarray:
    """3x3 binary erosion via shifts — keeps the module scipy-free."""
    m = mask
    for _ in range(iterations):
        p = np.pad(m, 1, mode="constant")
        m = (
            p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1]
            & p[1:-1, :-2] & p[1:-1, 2:]
            & p[:-2, :-2] & p[:-2, 2:] & p[2:, :-2] & p[2:, 2:]
        )
    return m


def _has_glare(grey: np.ndarray) -> bool:
    """Specular glare: near-white patches whose INTERIOR is texture-free.

    The mask is eroded before anything is measured so the patch boundary —
    which has steep gradients no matter what the patch is — never enters the
    statistic. An upper area bound separates glare (a patch) from a blown sky
    or white wall (a region): both are flat inside, only one is fixable by
    re-photographing the print.

    Both area bounds are fractions of the ERODED mask — see the constants for
    what that means for small patches, and why it is not a tuning target.
    """
    blown = _erode(grey >= GLARE_VALUE_MIN)
    frac = float(blown.mean())
    if not (GLARE_AREA_WARN <= frac <= GLARE_AREA_MAX):
        return False
    gy, gx = np.gradient(grey)
    grad = np.hypot(gx, gy)
    return float(grad[blown].std()) < GLARE_FLAT_STD


def _has_periodic_texture(grey: np.ndarray) -> bool:
    """Halftone dots / screen pixels / paper texture: an isolated spectral peak.

    Film grain and JPEG noise are broadband and do not trip this; a printed dot
    pattern or a screen's pixel grid concentrates energy in one or two sharp
    peaks that tower over the rest of the band.
    """
    h, w = grey.shape
    if h < 64 or w < 64:
        return False
    spec = np.abs(np.fft.rfft2(grey - grey.mean()))
    fy = np.fft.fftfreq(h)[:, None]     # handles rfft2's wrapped negative rows
    fx = np.fft.rfftfreq(w)[None, :]
    radius = np.hypot(fy, fx)
    band = spec[(radius > MOIRE_BAND_LO) & (radius < MOIRE_BAND_HI)]
    if band.size < 100:
        return False
    return float(band.max() / (np.percentile(band, 99.5) + 1e-6)) > MOIRE_PEAK_RATIO


def _detect_tone(img: Image.Image) -> tuple[bool, bool]:
    """Return (is_greyscale, is_sepia).

    Greyscale: the channels barely differ anywhere.
    Sepia: weakly saturated and the hue that is present clusters in the
    red-orange-yellow band typical of a toned print.
    """
    probe = _probe_copy(img, 512)
    arr = np.asarray(probe, dtype=np.float32)

    spread = arr.max(axis=2) - arr.min(axis=2)
    if float(spread.mean()) < GREY_MAX_SPREAD:
        return True, False

    hsv = np.asarray(probe.convert("HSV"), dtype=np.float32)
    hue, sat = hsv[:, :, 0], hsv[:, :, 1]
    mean_sat = float(sat.mean())
    if mean_sat > SEPIA_MAX_SAT:
        return False, False

    # Only consider pixels with enough saturation to have a meaningful hue.
    meaningful = sat > 20
    if meaningful.sum() < hue.size * 0.05:
        # Almost no colour anywhere — treat as greyscale rather than sepia.
        return True, False

    in_band = ((hue >= SEPIA_HUE_LO) & (hue <= SEPIA_HUE_HI))[meaningful]
    return False, bool(in_band.mean() > 0.75)


def _sharpness(img: Image.Image) -> float:
    """Variance of the Laplacian, measured at a normalised size.

    Higher is sharper. Computed with numpy slicing rather than scipy so the
    Lambda layer stays small.
    """
    probe = _probe_copy(img, SHARPNESS_PROBE_EDGE).convert("L")
    a = np.asarray(probe, dtype=np.float32)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    lap = (
        4.0 * a[1:-1, 1:-1]
        - a[:-2, 1:-1]
        - a[2:, 1:-1]
        - a[1:-1, :-2]
        - a[1:-1, 2:]
    )
    return float(lap.var())


def _probe_copy(img: Image.Image, edge: int) -> Image.Image:
    """A downscaled copy used for measurement only, never for output."""
    probe = img.copy()
    probe.thumbnail((edge, edge), RESAMPLE_FAST)
    return probe


def _scale_factor(w: int, h: int, frame_mode: str) -> float:
    """How far the source must be scaled to fill 1280x720 under this framing."""
    if frame_mode == "crop":
        # We keep full width and crop height (or vice versa) to reach 16:9,
        # then scale that region up to the target.
        if w / h >= TARGET_ASPECT:
            usable_w = h * TARGET_ASPECT
            return TARGET_W / usable_w
        return TARGET_W / w
    # Letterboxed: the image only has to fit inside the frame.
    return min(TARGET_W / w, TARGET_H / h)


# ── Framing ──────────────────────────────────────────────────────────────────


def apply_crop_rect(img: Image.Image, rect: dict) -> Image.Image:
    """Apply a normalised crop rectangle from the upload UI.

    `rect` is {"x": 0.0-1.0, "y": 0.0-1.0, "w": 0.0-1.0, "h": 0.0-1.0} in
    fractions of the source, so it stays valid regardless of what resolution the
    browser previewed at. Values are clamped rather than rejected — a slightly
    out-of-range rectangle from a drag gesture should not fail an order.
    """
    w, h = img.size
    x0 = max(0.0, min(1.0, float(rect.get("x", 0.0))))
    y0 = max(0.0, min(1.0, float(rect.get("y", 0.0))))
    rw = max(0.0, min(1.0 - x0, float(rect.get("w", 1.0))))
    rh = max(0.0, min(1.0 - y0, float(rect.get("h", 1.0))))

    if rw <= 0 or rh <= 0:
        logger.warning("Degenerate crop rect %s — ignoring", rect)
        return img

    box = (int(x0 * w), int(y0 * h), int((x0 + rw) * w), int((y0 + rh) * h))
    if box[2] - box[0] < 16 or box[3] - box[1] < 16:
        logger.warning("Crop rect %s too small — ignoring", rect)
        return img
    return img.crop(box)


def _crop_offset_16x9(img: Image.Image, faces: Optional[list]) -> tuple:
    """Choose the 16:9 crop window: (left, top, cw, ch, all_faces_fit).

    With face boxes the window slides to keep every face (plus hair/chin
    margin) in frame; without them it falls back to the centred window with
    the above-centre bias. Shared by the enhanced frame and the _before
    control so both are framed IDENTICALLY — the review email compares them
    side by side, which only means something if the framing matches.
    """
    w, h = img.size
    if w / h >= TARGET_ASPECT:
        cw, ch = int(round(h * TARGET_ASPECT)), h
    else:
        cw, ch = w, int(round(w / TARGET_ASPECT))

    if not faces:
        if w / h >= TARGET_ASPECT:
            return (w - cw) // 2, 0, cw, ch, True
        return 0, int((h - ch) * 0.4), cw, ch, True

    xs1, ys1, xs2, ys2 = [], [], [], []
    for x1, y1, x2, y2 in faces:
        fw, fh = x2 - x1, y2 - y1
        xs1.append(max(0.0, x1 - fw * FACE_MARGIN_X))
        xs2.append(min(float(w), x2 + fw * FACE_MARGIN_X))
        ys1.append(max(0.0, y1 - fh * FACE_MARGIN_TOP))
        ys2.append(min(float(h), y2 + fh * FACE_MARGIN_BOTTOM))
    ux1, uy1, ux2, uy2 = min(xs1), min(ys1), max(xs2), max(ys2)
    cx, cy = (ux1 + ux2) / 2, (uy1 + uy2) / 2

    left = int(np.clip(cx - cw / 2, 0, w - cw))
    top = int(np.clip(cy - ch / 2, 0, h - ch))
    fits = (ux2 - ux1) <= cw + 2 and (uy2 - uy1) <= ch + 2
    return left, top, cw, ch, fits


# How much margin a face needs around its detection box before a crop edge is
# considered to have "cut" it. Above the box covers hair; below keeps the chin
# clear of the frame edge.
FACE_MARGIN_X = 0.30
FACE_MARGIN_TOP = 0.60
FACE_MARGIN_BOTTOM = 0.40


def _centre_crop_16x9(img: Image.Image) -> Image.Image:
    """Crop the largest centred 16:9 region (no-information fallback path)."""
    left, top, cw, ch, _ = _crop_offset_16x9(img, None)
    return img.crop((left, top, left + cw, top + ch))


def _fit_with_blur_pad(img: Image.Image) -> Image.Image:
    """Letterbox into 1280x720 over a blurred, darkened copy of the frame.

    Used for portrait sources. A blurred fill reads as a deliberate style choice
    (the convention on every vertical-video platform), whereas black bars read as
    a mistake — and unlike AI outpainting it invents nothing.
    """
    bg = ImageOps.fit(img, (TARGET_W, TARGET_H), method=RESAMPLE)
    bg = bg.filter(ImageFilter.GaussianBlur(PAD_BLUR_RADIUS))
    bg = Image.blend(Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0)), bg, PAD_DARKEN)

    fg = img.copy()
    fg.thumbnail((TARGET_W, TARGET_H), RESAMPLE)
    bg.paste(fg, ((TARGET_W - fg.size[0]) // 2, (TARGET_H - fg.size[1]) // 2))
    return bg


# ── Tone ─────────────────────────────────────────────────────────────────────


def _stretch_luminance(arr: np.ndarray, strength: float) -> np.ndarray:
    """Percentile-stretch brightness only, leaving colour ratios intact.

    Recovers contrast in a faded print without neutralising its tint, which is
    what a per-channel stretch would do. Keeps sepia sepia.
    """
    luma = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    lo, hi = np.percentile(luma, [STRETCH_LO_PCT, STRETCH_HI_PCT])
    if hi - lo < STRETCH_MIN_RANGE:
        return arr

    target = np.clip((luma - lo) * 255.0 / (hi - lo), 0, 255)
    gain = target / np.maximum(luma, 1.0)
    gain = np.clip(gain, 0.0, 3.0)[:, :, None]

    out = np.clip(arr * gain, 0, 255)
    return strength * out + (1.0 - strength) * arr


def _stretch_per_channel(arr: np.ndarray, strength: float) -> np.ndarray:
    """Percentile-stretch each channel independently.

    Corrects the colour cast that scanned or aged colour prints pick up. Only
    applied to images that are genuinely colour — see `preserves_tint`.
    """
    out = arr.copy()
    for c in range(3):
        lo, hi = np.percentile(out[:, :, c], [STRETCH_LO_PCT, STRETCH_HI_PCT])
        if hi - lo < STRETCH_MIN_RANGE:
            continue
        out[:, :, c] = np.clip((out[:, :, c] - lo) * 255.0 / (hi - lo), 0, 255)
    return strength * out + (1.0 - strength) * arr


def _tone_correct(img: Image.Image, a: ImageAssessment, strength: float) -> Image.Image:
    if strength <= 0:
        return img
    arr = np.asarray(img, dtype=np.float32)
    if a.preserves_tint:
        out = _stretch_luminance(arr, strength)
    else:
        out = _stretch_per_channel(arr, strength)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


# ── Sharpening ───────────────────────────────────────────────────────────────


def _unsharp_percent(a: ImageAssessment) -> int:
    """Scale sharpening to how much the image was stretched and how soft it is.

    An upscaled frame loses acutance in proportion to the upscale, and a soft
    source needs more help than a crisp one — but the threshold in the filter
    keeps this from turning grain into speckle.
    """
    pct = UNSHARP_PERCENT_BASE
    if a.scale_factor > 1.0:
        pct += int((a.scale_factor - 1.0) * 55)
    if a.sharpness < SHARPNESS_SOFT:
        pct += 25
    return int(min(pct, UNSHARP_PERCENT_MAX))


def _transform_faces(
    faces: Optional[list], off_x: int, off_y: int, scale: float, size: tuple
) -> Optional[list]:
    """Map source-coordinate face boxes into the framed/prescaled image.

    Preserves the None/[] distinction (unavailable vs none found). Boxes whose
    centre falls outside the framed region are dropped — a face cropped out of
    frame must not influence the restore weight.
    """
    if faces is None:
        return None
    w, h = size
    out = []
    for x1, y1, x2, y2 in faces:
        nx1, ny1 = (x1 - off_x) * scale, (y1 - off_y) * scale
        nx2, ny2 = (x2 - off_x) * scale, (y2 - off_y) * scale
        cx, cy = (nx1 + nx2) / 2, (ny1 + ny2) / 2
        if not (0 <= cx < w and 0 <= cy < h):
            continue
        out.append([
            max(0.0, nx1), max(0.0, ny1), min(float(w), nx2), min(float(h), ny2)
        ])
    return out


# ── Public entry point ───────────────────────────────────────────────────────


def _frame(
    img: Image.Image,
    a: ImageAssessment,
    crop_rect: Optional[dict],
    faces: Optional[list] = None,
) -> tuple[Image.Image, tuple[int, int]]:
    """Reduce the source to the region that will actually be shown.

    Done before restoration so the slow models never process pixels that are
    about to be discarded. On a 3000px source cropped to 16:9 that is a quarter
    of the work saved.

    Returns (framed, (offset_x, offset_y)) — the offset of the framed region
    in source coordinates, so face boxes detected on the source can be
    transformed into the framed image for the restore stage.

    `faces` (source-coordinate boxes from the single detection pass in the
    handler) makes the automatic crop face-aware. A customer-chosen crop_rect
    always wins — the customer framed it deliberately — and only the
    exact-ratio touch-up runs.
    """
    if crop_rect:
        # The UI constrains the handle to 16:9, but clamping in apply_crop_rect
        # can shave it slightly — re-crop to exact ratio so the final resize
        # never distorts. Offsets compose: user rect, then ratio touch-up.
        w, h = img.size
        rected = apply_crop_rect(img, crop_rect)
        rx = int(max(0.0, min(1.0, float(crop_rect.get("x", 0.0)))) * w)
        ry = int(max(0.0, min(1.0, float(crop_rect.get("y", 0.0)))) * h)
        if rected.size == img.size:      # degenerate rect was ignored
            rx = ry = 0
        left, top, cw, ch, _ = _crop_offset_16x9(rected, None)
        return rected.crop((left, top, left + cw, top + ch)), (rx + left, ry + top)
    if a.frame_mode == "crop":
        left, top, cw, ch, fits = _crop_offset_16x9(img, faces)
        if faces and not fits:
            warn = (
                "faces_exceed_frame: the people in this photo span more "
                "than a 16:9 crop can hold — check the framing"
            )
            if warn not in a.warnings:   # _frame runs twice (frame + control)
                a.warnings.append(warn)
        return img.crop((left, top, left + cw, top + ch)), (left, top)
    return img, (0, 0)


def _prescale_for_restore(framed: Image.Image, a: ImageAssessment) -> Image.Image:
    """Downscale the framed region to the output size before restoration runs.

    Restoration is the most expensive step in the pipeline by a wide margin, and
    its cost scales with pixel count. Running it at source resolution and then
    downscaling to 1280x720 spends most of that cost on pixels that are
    immediately discarded — on a 3008MB Lambda that is the difference between
    working and being killed for running out of memory.

    Only ever shrinks. An image smaller than the target is left alone so the
    upscaler still has its original pixels to work from.

    The face model is unaffected in practice: GFPGAN crops each detected face to
    512x512 internally regardless of the frame it came from, and the montage is
    720p, so detail beyond that is not visible in the finished video.
    """
    fw, fh = framed.size
    if a.frame_mode == "crop":
        scale = TARGET_W / fw
    else:
        scale = min(TARGET_W / fw, TARGET_H / fh)

    if scale >= 1.0:
        return framed

    new = (max(1, round(fw * scale)), max(1, round(fh * scale)))
    logger.info("Pre-scaling %sx%s -> %sx%s before restoration", fw, fh, *new)
    return framed.resize(new, RESAMPLE)


def _reassess_for_region(a: ImageAssessment, framed: Image.Image) -> ImageAssessment:
    """Recompute scale need against the region that will actually be shown.

    `assess()` measures the whole photo, but restoration runs after framing. A
    user who crops tightly to a face turns a 3000px source into a 600px region
    that genuinely needs upscaling — measured on the full image that need is
    invisible. Without this, the tighter the crop the worse the output, which is
    exactly backwards.
    """
    fw, fh = framed.size
    if (fw, fh) == (a.width, a.height):
        return a

    scale = min(TARGET_W / fw, TARGET_H / fh) if a.frame_mode == "pad" else TARGET_W / fw
    needs_upscale = scale > UPSCALE_TRIGGER
    if needs_upscale and a.needs_face_restore:
        tier = 2
    elif a.needs_face_restore:
        tier = 2
    elif needs_upscale:
        tier = 1
    else:
        tier = 0

    return replace(
        a,
        scale_factor=round(scale, 3),
        needs_upscale=needs_upscale,
        tier=tier,
        reason=f"{a.reason} (re-measured on {fw}x{fh} framed region)",
    )


def prepare(
    img: Image.Image,
    assessment: Optional[ImageAssessment] = None,
    crop_rect: Optional[dict] = None,
    tone_strength: float = 0.6,
    sharpen: bool = True,
    restore=None,
    detect_faces=None,
    faces: Optional[list] = None,
) -> tuple[Image.Image, dict]:
    """Produce the 1280x720 RGB frame that gets submitted to Runway.

    Returns (frame, meta) where meta records what restoration actually ran.

    Order matters:
      frame    — discard unused pixels first, so nothing slow runs on them
      restore  — generative pass, on the real pixels, at source scale
      tone     — statistics are richest before downscaling
      resize   — Lanczos to the output frame
      sharpen  — last, so the filter acts at output scale

    `restore` is an optional callable (img, assessment) -> (img, meta). It is
    injected rather than imported so this module stays torch-free and testable;
    the container function passes `image_restore.restore`, and anything else
    passes nothing and gets the deterministic path.

    `faces` is an optional precomputed detection result in SOURCE coordinates
    (None = detection unavailable, [] = no faces, else boxes). The prep
    handler detects once and passes the same boxes to the enhanced frame, the
    _before control frame, and (transformed) the restore stage — one detector
    pass per photo, and every decision made from the same boxes.

    `detect_faces` is an optional callable (img) -> faces with the same
    contract, used only when `faces` is not supplied. Without either, framing
    and restoration behave exactly as before this feature existed.
    """
    a = assessment or assess(img)
    meta: dict = {"restored": False}

    if faces is None and detect_faces is not None:
        try:
            faces = detect_faces(img)
        except Exception:
            logger.warning("Face detection callback failed — centre crop", exc_info=True)
            faces = None

    framed, (off_x, off_y) = _frame(img, a, crop_rect, faces if not crop_rect else None)

    if restore is not None:
        # Shrink to the output size first — see _prescale_for_restore. The
        # reassessment must happen after, so needs_upscale reflects the pixels
        # restoration actually receives.
        pre_w = framed.size[0]
        framed = _prescale_for_restore(framed, a)
        scale = framed.size[0] / pre_w
        a_region = _reassess_for_region(a, framed)
        a_region = replace(a_region, faces_framed=_transform_faces(
            faces, off_x, off_y, scale, framed.size))
        framed, meta = restore(framed, a_region)

    toned = _tone_correct(framed, a, tone_strength)

    if crop_rect or a.frame_mode == "crop":
        out = toned.resize((TARGET_W, TARGET_H), RESAMPLE)
    else:
        out = _fit_with_blur_pad(toned)

    if sharpen:
        # A GFPGAN pass already returns crisp faces. Sharpening on top of that
        # produces halos around the eyes and jaw, so back the filter off when
        # restoration ran.
        percent = _unsharp_percent(a)
        if meta.get("restored"):
            percent = int(percent * 0.5)
        out = out.filter(
            ImageFilter.UnsharpMask(
                radius=UNSHARP_RADIUS, percent=percent, threshold=UNSHARP_THRESHOLD
            )
        )
    return out, meta


def to_jpeg_bytes(img: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    """Encode for upload. JPEG, not PNG — Runway fetches this over a presigned
    URL and a 4x-upscaled PNG can exceed 20MB for no visible benefit."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def prepare_bytes(
    data: bytes,
    crop_rect: Optional[dict] = None,
    restore=None,
    detect_faces=None,
    faces: Optional[list] = None,
) -> tuple[bytes, ImageAssessment, dict]:
    """Convenience wrapper: raw upload bytes in, Runway-ready JPEG bytes out."""
    img = load_and_orient(data)
    a = assess(img)
    out, meta = prepare(
        img, a, crop_rect=crop_rect, restore=restore,
        detect_faces=detect_faces, faces=faces,
    )
    return to_jpeg_bytes(out), a, meta
