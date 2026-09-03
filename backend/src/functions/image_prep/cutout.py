"""Cut the subject free of the background, for the montage's opening.

Produced here rather than in montage_builder because segmentation needs a model,
and this is the only container function in the stack — montage_builder is a zip
function on the shared layer and has no room for one.

The cut-out is made from the PREPARED frame, not the original. That matters: the
intro flies the cut-out in and then fades the photograph in behind it, and the
two only line up if they came from the same pixels. It is also why a mediocre
mask is survivable — at the moment of landing the cut-out sits exactly on top of
the region it was cut from, and the seam disappears.

Everything here is best-effort. A missing model, a failed import or an empty
mask returns None, and the photo simply flies in as a print instead.
"""

from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# Components smaller than this fraction of the frame are dropped outright: they
# are dust, mount board and JPEG noise, never a person.
MIN_COMPONENT = 0.0015
EDGE_FEATHER = 1.2

_SESSION = None
_UNAVAILABLE = False


def _session():
    """Load the segmentation model once per container, never at import time.

    Import-time loading would add its cost to every image_prep cold start,
    including the many that never need a cut-out.
    """
    global _SESSION, _UNAVAILABLE
    if _SESSION is not None or _UNAVAILABLE:
        return _SESSION
    try:
        from rembg import new_session
        _SESSION = new_session(os.environ.get("CUTOUT_MODEL", "isnet-general-use"))
    except Exception as e:                       # noqa: BLE001
        logger.warning("Cut-out model unavailable (%s) — intro will use prints only", e)
        _UNAVAILABLE = True
    return _SESSION


def make_cutout(prepared_rgb, faces=None) -> bytes | None:
    """RGBA PNG of the subject, or None if no usable mask could be made.

    `faces` is the RetinaFace output image_prep already computed for framing —
    reused here rather than detected again.
    """
    session = _session()
    if session is None:
        return None
    try:
        from rembg import remove
        from PIL import Image
        from scipy import ndimage
    except Exception as e:                       # noqa: BLE001
        logger.warning("Cut-out dependencies missing (%s)", e)
        return None

    try:
        img = prepared_rgb if hasattr(prepared_rgb, "size") else Image.fromarray(prepared_rgb)
        rgba = np.array(remove(img.convert("RGB"), session=session, post_process_mask=True))
        alpha = rgba[..., 3]
        labels, n = ndimage.label(alpha > 40)
        if n == 0:
            return None

        h, w = alpha.shape
        keep = _face_gated(labels, n, faces, w, h)
        if not keep:
            sizes = ndimage.sum(alpha > 40, labels, range(1, n + 1))
            keep = {int(np.argmax(sizes)) + 1}

        mask = np.isin(labels, list(keep))
        if mask.mean() < MIN_COMPONENT:
            return None
        rgba[..., 3] = np.where(mask, alpha, 0)

        try:                                     # soften the edge; cosmetic only
            import cv2
            rgba[..., 3] = cv2.GaussianBlur(rgba[..., 3], (0, 0), EDGE_FEATHER)
        except Exception:                        # noqa: BLE001
            pass

        import io
        buf = io.BytesIO()
        Image.fromarray(rgba).save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:                       # noqa: BLE001
        logger.warning("Cut-out failed: %s", e)
        return None


def _face_gated(labels, n: int, faces, w: int, h: int) -> set:
    """Keep only mask components that contain a detected face.

    This is what removes the armchair. Background segmentation happily returns
    the sofa a family is sitting on, the bouquet in front of them and the
    caption printed along the bottom of the scan; all of those are components
    with no face in them. Sampling at three heights down the face box catches
    the body, since the head is often its own component after a dark collar.

    Deliberately NOT combined with a "keep anything above X% of the largest
    component" rule — that reinstates exactly the furniture this removes.
    """
    keep: set = set()
    for box in _boxes(faces):
        x, y, bw, bh = box
        for frac in (0.45, 0.6, 0.8):
            yy = int(min(h - 1, max(0, y + bh * frac)))
            xx = int(min(w - 1, max(0, x + bw * 0.5)))
            lab = int(labels[yy, xx])
            if lab:
                keep.add(lab)
    return keep


def _boxes(faces):
    """Normalise whatever the detector returned into (x, y, w, h) tuples.

    RetinaFace gives corner boxes with a confidence; other detectors give
    width/height. Both shapes appear in this codebase depending on which
    detector was available, so accept either rather than trusting one.
    """
    if faces is None:
        return []
    out = []
    for f in faces:
        try:
            vals = [float(v) for v in (f[:4] if len(f) >= 4 else [])]
        except (TypeError, ValueError, IndexError):
            continue
        if len(vals) < 4:
            continue
        x1, y1, a, b = vals
        # corner box (x1, y1, x2, y2) if the last two are larger than the first two
        if a > x1 and b > y1 and (a - x1) < 10000:
            out.append((x1, y1, a - x1, b - y1))
        else:
            out.append((x1, y1, a, b))
    return out
