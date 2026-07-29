"""
Generative image restoration — Real-ESRGAN upscaling and GFPGAN face restoration.

This is the half of image preparation that needs torch, and therefore the reason
`ImagePrepFunction` ships as a container image instead of a zip. Everything here
is import-guarded: on any runtime without torch the module imports cleanly and
`is_available()` returns False, so the deterministic pipeline in `image_prep`
keeps working unchanged.

WHAT THIS DOES THAT DETERMINISTIC PROCESSING CANNOT
Percentile stretching and unsharp masking recover detail that is present but
muddy. They cannot recover detail the print never recorded. GFPGAN synthesises
facial detail from a learned prior, which is why it transforms a soft scan where
a sharpening filter merely cleans it.

WHAT THAT COSTS
The same prior that reconstructs a 1966 face can invent one. Three things keep
that in bounds:

  1. It only runs when measured as necessary — see `ImageAssessment.tier`. A
     sharp modern photo never reaches this module.
  2. `RESTORE_WEIGHT` blends the reconstruction back toward the real face.
  3. The unmodified source is kept in S3 next to the result, so every decision
     is auditable against the original.

Models are baked into the image at /opt/ml/models and inside site-packages —
see the Dockerfile. Nothing is downloaded at runtime.
"""

from __future__ import annotations

import gc
import logging
import os
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/ml/models")

# How far the reconstructed face is blended back toward the original.
# GFPGAN's own default is 0.5. Higher means more reconstruction and more risk of
# subtly altering a face the customer knows intimately.
RESTORE_WEIGHT = float(os.environ.get("RESTORE_WEIGHT", "0.5"))

# Real-ESRGAN processes in tiles to bound peak memory. Each tile expands 4x
# through a 64-channel network, so this value sets peak allocation almost on its
# own: 200px keeps a tile's working set to roughly a few hundred MB, which fits
# alongside the loaded models on a 3008MB function. Raise only with headroom to
# spare.
ESRGAN_TILE = int(os.environ.get("ESRGAN_TILE", "200"))

# The upscaler is x4 natively; asking for more than this on a memorial photo
# produces obvious plastic texture rather than detail.
MAX_UPSCALE = 4.0

ENABLE_RESTORE = os.environ.get("ENABLE_RESTORE", "true").lower() == "true"

# Minimum function memory needed to run restoration at all.
#
# Measured, not guessed: loading torch, GFPGAN and the facexlib detector costs
# ~1.2GB, and a single enhance() call adds ~2.6GB on top. That spike comes from
# the RetinaFace detector and is FIXED — it does not shrink with input size, so
# there is no small-enough image that makes restoration fit in a smaller
# function. Peak lands around 3.8GB.
#
# This matters because a Lambda OOM is a process kill, not a Python exception:
# it cannot be caught, so the graceful fallback inside restore() never gets to
# run and the invocation dies outright. Checking up front is the only way to
# degrade cleanly to deterministic-only processing.
#
# The check reads the live function memory, so raising MemorySize in
# template.yaml (once the account's Lambda memory quota allows it) turns
# restoration on by itself with no code change.
MIN_RESTORE_MEMORY_MB = int(os.environ.get("MIN_RESTORE_MEMORY_MB", "4096"))


def _function_memory_mb() -> int:
    """Memory allocated to this Lambda, or 0 when not running on Lambda."""
    try:
        return int(os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "0"))
    except ValueError:
        return 0


def has_enough_memory() -> bool:
    """False when this function is too small to survive a restoration pass."""
    mem = _function_memory_mb()
    return mem == 0 or mem >= MIN_RESTORE_MEMORY_MB

# Torch is single-process here; left unbounded it oversubscribes Lambda's vCPUs
# and gets slower, not faster.
_TORCH_THREADS = int(os.environ.get("TORCH_THREADS", "4"))

_upsampler = None
_restorer_cache: dict = {}
_available: Optional[bool] = None


# ── Availability ─────────────────────────────────────────────────────────────


def _disable_grad() -> None:
    """Turn off autograd process-wide.

    Every tensor operation otherwise retains the intermediate activations needed
    for a backward pass that never happens. In a 23-block RRDBNet with 64
    feature channels those activations are the single largest consumer of
    memory in this function — far larger than the model weights. Set globally
    rather than as a context manager so it also covers the model construction
    and any library code that does not wrap itself.
    """
    import torch

    torch.set_grad_enabled(False)


def is_available() -> bool:
    """True when the torch stack imported successfully.

    Cached: the import is expensive and the answer cannot change within a
    container lifetime.
    """
    global _available
    if _available is not None:
        return _available
    try:
        import torch  # noqa: F401
        from gfpgan import GFPGANer  # noqa: F401
        from realesrgan import RealESRGANer  # noqa: F401

        _available = True
    except Exception as e:
        logger.warning("Restoration stack unavailable (%s) — deterministic only", e)
        _available = False
    return _available


# ── Model construction (lazy, cached across warm invocations) ────────────────


def _get_upsampler():
    """Real-ESRGAN x4plus. Built once per container, reused across invocations."""
    global _upsampler
    if _upsampler is not None:
        return _upsampler

    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer

    torch.set_num_threads(_TORCH_THREADS)
    _disable_grad()

    arch = RRDBNet(
        num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4
    )
    _upsampler = RealESRGANer(
        scale=4,
        model_path=os.path.join(MODEL_DIR, "RealESRGAN_x4plus.pth"),
        model=arch,
        tile=ESRGAN_TILE,
        tile_pad=10,
        pre_pad=0,
        half=False,          # no fp16 on CPU
        device="cpu",
    )
    logger.info("Real-ESRGAN loaded")
    return _upsampler


def _get_restorer(upscale: int, with_background: bool):
    """GFPGAN v1.4.

    GFPGANer bakes the upscale factor into the instance, so we cache one per
    (upscale, background) combination rather than rebuilding per image.
    """
    key = (upscale, with_background)
    if key in _restorer_cache:
        return _restorer_cache[key]

    from gfpgan import GFPGANer

    _disable_grad()
    restorer = GFPGANer(
        model_path=os.path.join(MODEL_DIR, "GFPGANv1.4.pth"),
        upscale=upscale,
        arch="clean",
        channel_multiplier=2,
        bg_upsampler=_get_upsampler() if with_background else None,
        device="cpu",
    )
    _restorer_cache[key] = restorer
    logger.info("GFPGAN loaded (upscale=%s background=%s)", upscale, with_background)
    return restorer


# ── Conversion helpers ───────────────────────────────────────────────────────


def _pil_to_bgr(img: Image.Image) -> np.ndarray:
    """PIL is RGB, the whole GFPGAN/ESRGAN stack is OpenCV BGR."""
    return np.asarray(img.convert("RGB"))[:, :, ::-1].copy()


def _bgr_to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(arr[:, :, ::-1].copy(), "RGB")


def _force_greyscale(img: Image.Image) -> Image.Image:
    """Strip colour that the face prior introduced.

    GFPGAN is trained on colour faces and will tint a black-and-white portrait
    with plausible skin tones. On a memorial video that reads as a colourised
    photo nobody asked for, so monochrome sources are forced back to monochrome.
    """
    return img.convert("L").convert("RGB")


def _target_upscale(assessment) -> int:
    """Integer upscale factor to feed the models.

    Both models take an integer factor, so we round up to guarantee we reach the
    output resolution and let the Lanczos downscale in `image_prep` land it
    exactly. Rounding down would leave us upscaling twice.
    """
    if not assessment.needs_upscale:
        return 1
    return int(min(MAX_UPSCALE, max(1, np.ceil(assessment.scale_factor))))


# ── Public API ───────────────────────────────────────────────────────────────


def restore(img: Image.Image, assessment) -> tuple[Image.Image, dict]:
    """Apply whatever restoration this image was measured to need.

    Signature matches the `restore` callback `image_prep.prepare` expects.
    Never raises: any failure logs and returns the input untouched, because a
    slightly soft photo in the finished video is vastly better than a failed
    order.
    """
    meta = {
        "restored": False,
        "tier": assessment.tier,
        "upscale": 1,
        "face_restore": False,
        "weight": RESTORE_WEIGHT,
    }

    if not ENABLE_RESTORE:
        meta["skipped"] = "disabled by ENABLE_RESTORE"
        return img, meta
    if not has_enough_memory():
        # Checked before any model loads. An OOM here would kill the process
        # outright rather than raising, taking the whole invocation with it.
        msg = (
            f"function has {_function_memory_mb()}MB, restoration needs "
            f"{MIN_RESTORE_MEMORY_MB}MB — deterministic processing only"
        )
        logger.warning(msg)
        meta["skipped"] = msg
        return img, meta
    if assessment.tier == 0:
        meta["skipped"] = "tier 0 — no restoration needed"
        return img, meta
    if not is_available():
        meta["skipped"] = "torch stack unavailable"
        return img, meta

    upscale = _target_upscale(assessment)
    meta["upscale"] = upscale
    logger.info(
        "Restoring %sx%s tier=%s upscale=%s face=%s",
        img.size[0], img.size[1], assessment.tier, upscale,
        assessment.needs_face_restore,
    )

    try:
        # The two models are run as separate, individually-bounded steps rather
        # than letting GFPGAN drive Real-ESRGAN as its background upsampler.
        # GFPGANer(upscale=N, bg_upsampler=...) runs the 4x network across the
        # whole frame in one unbounded pass, which is what exhausted memory on a
        # 3008MB function. Split, each step is tiled or native-resolution and
        # peak memory stays flat.
        out = img
        if assessment.needs_face_restore:
            out = _run_face_restore(out)
            meta["face_restore"] = True
        if assessment.needs_upscale:
            out = _run_upscale_only(out, upscale)

        # GFPGAN tints monochrome faces — put that back before anything
        # downstream sees it.
        if assessment.is_greyscale:
            out = _force_greyscale(out)

        meta["restored"] = True
        meta["output_size"] = list(out.size)
        logger.info(
            "Restored %sx%s -> %sx%s (tier=%s upscale=%s face=%s)",
            *img.size, *out.size, assessment.tier, upscale, meta["face_restore"],
        )
        return out, meta

    except Exception as e:
        logger.error("Restoration failed, using unrestored image: %s", e, exc_info=True)
        meta["error"] = str(e)
        return img, meta

    finally:
        # Torch holds onto large intermediate buffers until the allocator is
        # nudged. The models themselves stay cached for the next invocation —
        # only the per-image tensors are released.
        gc.collect()


def _run_face_restore(img: Image.Image) -> Image.Image:
    """GFPGAN at native resolution, faces only.

    Always upscale=1 with no background upsampler. GFPGAN crops each detected
    face to 512x512, restores it, and pastes it back, so the frame size only
    affects detection cost — the restoration quality is the same either way, and
    any resolution change is handled far more cheaply downstream.

    `only_center_face=False` matters here: memorial photos are frequently group
    shots, and restoring one face in a family portrait looks worse than
    restoring none.
    """
    restorer = _get_restorer(upscale=1, with_background=False)
    _, _, output = restorer.enhance(
        _pil_to_bgr(img),
        has_aligned=False,
        only_center_face=False,
        paste_back=True,
        weight=RESTORE_WEIGHT,
    )
    if output is None:
        raise RuntimeError("GFPGAN returned no output")
    return _bgr_to_pil(output)


def _run_upscale_only(img: Image.Image, upscale: int) -> Image.Image:
    """Real-ESRGAN with no face model — for images that are sharp but small."""
    output, _ = _get_upsampler().enhance(_pil_to_bgr(img), outscale=upscale)
    if output is None:
        raise RuntimeError("Real-ESRGAN returned no output")
    return _bgr_to_pil(output)
