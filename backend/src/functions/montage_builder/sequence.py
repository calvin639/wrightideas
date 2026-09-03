"""Intro and outro sequences for the montage.

Pure PIL frame generation — no torch, no network, nothing the ffmpeg layer does
not already provide. Frames are yielded as raw RGB bytes so the handler can pipe
them straight into ffmpeg rather than writing a few hundred JPEGs to /tmp.

Two intro styles, both ending on the SAME arrangement of prints (the "wall"), so
the outro is shared:

  wall      Each subject steps out of the dark at centre screen, holds a beat,
            then travels to its place — where its own photograph fades in behind
            it, in exact register, and it settles back into the picture. Because
            the cut-out sits on the pixels it was cut from, the landing hides
            every segmentation error. Photos with no cut-out simply fly in as
            prints.

  flipbook  The whole album riffles past the lens as a stack of prints, then
            deals itself out onto the wall.

The outro rewinds the montage at speed and lands back on the wall.
"""

from __future__ import annotations

import math
from typing import Iterator, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H, FPS = 1280, 720, 24
BG = (11, 11, 14)
GOLD = (206, 176, 138)
CREAM = (243, 238, 230)
BORDER = (238, 234, 226)

try:
    from shared.models import WALL_INTRO, FLIPBOOK_INTRO, INTRO_STYLES
except ImportError:          # standalone use outside the Lambda package
    WALL_INTRO = "wall"
    FLIPBOOK_INTRO = "flipbook"
    INTRO_STYLES = (WALL_INTRO, FLIPBOOK_INTRO)

# The wall stays legible at about a dozen prints. Beyond that each print shrinks
# to a postage stamp and no face survives, so larger orders put their best
# candidates on the wall and the rest appear in the body only.
MAX_WALL = 12

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/opt/fonts",
    "/var/task/fonts",
)


def _font(size: int, bold: bool = False):
    stem = f"DejaVuSerif{'-Bold' if bold else ''}.ttf"
    alt = f"DejaVuSans{'-Bold' if bold else ''}.ttf"
    for d in _FONT_DIRS:
        for name in (stem, alt):
            try:
                return ImageFont.truetype(f"{d}/{name}", size)
            except OSError:
                continue
    return ImageFont.load_default()


def _ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def _ease_io(t: float) -> float:
    return 3 * t * t - 2 * t ** 3


_yy, _xx = np.mgrid[0:H, 0:W]
_dist = np.sqrt(((_xx - W / 2) / (W / 2)) ** 2 + ((_yy - H / 2) / (H / 2)) ** 2)
VIGNETTE = np.clip(1.0 - 0.55 * np.clip(_dist - 0.55, 0, None) ** 1.6, 0, 1)[..., None].astype(np.float32)


def _finish(canvas: Image.Image, dim: float = 0.0) -> Image.Image:
    a = np.asarray(canvas.convert("RGB")).astype(np.float32) * VIGNETTE
    if dim:
        a *= (1.0 - dim)
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


def framed(tile: Image.Image, b: int = 10) -> Image.Image:
    """A photo as a physical print: thin border, ready for a soft shadow."""
    card = Image.new("RGBA", (tile.width + 2 * b, tile.height + 2 * b), BORDER + (255,))
    card.paste(tile.convert("RGB"), (b, b))
    return card


def _text_layer(lines) -> Image.Image:
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    for txt, font, colour, y in lines:
        if not txt:
            continue
        bb = d.textbbox((0, 0), txt, font=font)
        d.text(((W - (bb[2] - bb[0])) / 2 - bb[0], y), txt, font=font, fill=colour + (255,))
    return lay


class _Compositor:
    """Draws 1280x720-registered layers under an affine transform.

    Sprites are cached because the wall is redrawn on every frame and the same
    card recurs at the same scale for hundreds of them. Without the cache this
    is the difference between a montage that fits in a Lambda invocation and one
    that does not.
    """

    def __init__(self, cache_limit: int = 600):
        self._cache: dict = {}
        self._limit = cache_limit

    def _sprite(self, layer, key, scale, rot, blur, shadow):
        ck = (key, round(scale, 4), round(rot, 2), round(blur, 1), shadow)
        hit = self._cache.get(ck)
        if hit is not None:
            return hit
        im = layer.resize((max(2, int(layer.width * scale)), max(2, int(layer.height * scale))),
                          Image.BILINEAR)
        if blur > 0.4:
            im = im.filter(ImageFilter.GaussianBlur(blur))
        if abs(rot) > 0.01:
            im = im.rotate(rot, resample=Image.BILINEAR, expand=True)
        im = im.convert("RGBA")
        sh = None
        if shadow:
            sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
            sh.putalpha(im.getchannel("A").point(lambda v: int(v * 0.55)))
            sh = sh.filter(ImageFilter.GaussianBlur(14))
        if len(self._cache) < self._limit:
            self._cache[ck] = (im, sh)
        return im, sh

    def place(self, canvas, layer, scale, cx, cy, rot, alpha=1.0, blur=0.0, shadow=True, key=None):
        if alpha <= 0.004:
            return
        im, sh = self._sprite(layer, key if key is not None else id(layer), scale, rot, blur, shadow)
        if alpha < 1.0:
            im = im.copy()
            im.putalpha(im.getchannel("A").point(lambda v: int(v * alpha)))
            if sh is not None:
                sh = sh.copy()
                sh.putalpha(sh.getchannel("A").point(lambda v: int(v * alpha)))
        x, y = int(cx - im.width / 2), int(cy - im.height / 2)
        if sh is not None:
            canvas.alpha_composite(sh, (x + 4, y + 10))
        canvas.alpha_composite(im, (x, y))


def _rows_for(n: int) -> list:
    """Masonry rows that cover a 16:9 frame without leaving holes."""
    if n <= 3:
        return [n]
    if n <= 6:
        return [n // 2 + n % 2, n // 2]
    r = max(2, min(3, round(math.sqrt(n / 1.6))))
    per = [n // r] * r
    for k in range(n - sum(per)):
        per[k % r] += 1
    return per


def build_layout(items: list, seed: int = 11) -> list:
    """Give every wall item a home: scale, centre, tilt.

    Positions are pushed past the frame edges so the pile bleeds off screen
    instead of floating in a box of background.
    """
    rng = np.random.default_rng(seed)
    rows = _rows_for(len(items))
    tile_w = W * (0.485 if len(items) <= 10 else 0.445)
    k = 0
    for r, count in enumerate(rows):
        for c in range(count):
            if k >= len(items):
                break
            it = items[k]
            k += 1
            base_x = W * (c + 0.5) / count
            base_y = H * (r + 0.5) / len(rows)
            it["scale"] = (tile_w / W) * float(rng.uniform(0.97, 1.06))
            it["cx"] = W / 2 + (base_x - W / 2) * 1.10 + float(rng.uniform(-0.02, 0.02)) * W
            it["cy"] = H / 2 + (base_y - H / 2) * 1.13 + float(rng.uniform(-0.03, 0.03)) * H
            it["rot"] = float(rng.uniform(-3.4, 3.4))
            it["card"] = framed(it["tile"])
            it["card_scale"] = it["scale"] * (it["card"].width / W)
    return items


def _on_wall(items: list) -> list:
    """The subset that has a home on the wall. Items are flagged upstream; if
    nothing is flagged (a small order) they all belong."""
    wall = [it for it in items if it.get("on_wall")]
    return wall or items


def wall_image(items: list, comp: _Compositor, dim: float = 0.0) -> Image.Image:
    canvas = Image.new("RGBA", (W, H), BG + (255,))
    for it in _on_wall(items):
        comp.place(canvas, it["card"], it["card_scale"], it["cx"], it["cy"], it["rot"],
                   key=("card", it["key"]))
    return _finish(canvas, dim)


HERO = 0.58          # a subject's size when it holds at centre screen
ENTRY_MULT = 1.62    # ... and how much larger it is as it comes past the lens


def _title_layer(name: str, message: str) -> Image.Image:
    return _text_layer([
        (name, _font(92, True), CREAM, H / 2 - (110 if message else 60)),
        (message, _font(40), GOLD, H / 2 + 40),
    ])


def render_intro(items: list, name: str, message: str = "",
                 style: str = WALL_INTRO) -> Iterator[Image.Image]:
    """Yield the intro frames for the chosen style."""
    if style == FLIPBOOK_INTRO:
        yield from _render_flipbook(items, name, message)
    else:
        yield from _render_wall(items, name, message)


def _render_wall(items, name, message) -> Iterator[Image.Image]:
    items = _on_wall(items)
    comp = _Compositor()
    title_in, title_hold = 1.5, 1.4
    # Arrivals accelerate, so a twelve-photo intro lands in about the same time
    # as a six-photo one instead of dragging.
    stag = np.linspace(0.60, 0.30, max(1, len(items)))
    starts, t = [], title_in + title_hold
    for s in stag:
        starts.append(t)
        t += float(s)
    A, B, C, D = 0.26, 0.38, 0.80, 0.98      # phase boundaries within one arrival
    total = starts[-1] + D + 1.6
    title = _title_layer(name, message)

    base = Image.new("RGBA", (W, H), BG + (255,))
    landed_n = 0
    for fi in range(int(total * FPS)):
        t = fi / FPS
        landed = sum(1 for s in starts if t >= s + D)
        while landed_n < landed:
            it = items[landed_n]
            comp.place(base, it["card"], it["card_scale"], it["cx"], it["cy"], it["rot"],
                       key=("card", it["key"]))
            landed_n += 1
        canvas = base.copy()

        for it, s in zip(items, starts):
            dt = t - s
            if dt < 0 or dt >= D:
                continue
            subject = it.get("cutout") or it["card"]
            skey = ("cut", it["key"]) if it.get("cutout") else ("card", it["key"])
            hero = HERO if it.get("cutout") else it["card_scale"] * 1.55
            entry = hero * ENTRY_MULT
            if dt < A:                                   # in from behind the lens
                u = _ease_out(dt / A)
                sc = entry + (hero - entry) * u
                cx, cy, rot, al, bl = W / 2, H / 2 * 0.99, 0.0, min(1.0, u * 1.6), 7 * (1 - u)
            elif dt < B:                                 # the beat where you see them
                sc, cx, cy, rot, al, bl = hero, W / 2, H / 2 * 0.99, 0.0, 1.0, 0.0
            else:                                        # away to their place
                u = _ease_io(min(1.0, (dt - B) / (C - B)))
                target = it["scale"] if it.get("cutout") else it["card_scale"]
                sc = hero + (target - hero) * u
                cx = W / 2 + (it["cx"] - W / 2) * u
                cy = H / 2 * 0.99 + (it["cy"] - H / 2 * 0.99) * u
                rot, al, bl = it["rot"] * u, 1.0, 0.0
                if dt >= C and it.get("cutout"):
                    # the photograph arrives behind them, in register
                    comp.place(canvas, it["card"], it["card_scale"], it["cx"], it["cy"],
                               it["rot"], alpha=(dt - C) / (D - C), key=("card", it["key"]))
            comp.place(canvas, subject, sc, cx, cy, rot, alpha=al, blur=bl,
                       shadow=not it.get("cutout"), key=skey)

        frame = _finish(canvas)
        if t < starts[0] + 0.5:
            frame = _overlay_title(comp, frame, title, t, title_in, starts[0], 0.5)
        yield frame


def _render_flipbook(items, name, message) -> Iterator[Image.Image]:
    comp = _Compositor()
    # Every photo riffles past, not just the wall subset — nothing is left out.
    deck = items
    cards = {it["key"]: it["card"] for it in deck}
    card_ratio = next(iter(cards.values())).width / W
    rng = np.random.default_rng(23)
    jit = {it["key"]: (float(rng.uniform(-2.6, 2.6)), float(rng.uniform(-26, 26)),
                       float(rng.uniform(-18, 18))) for it in deck}

    title_in, title_hold = 1.4, 1.2
    t0 = title_in + title_hold
    n = len(deck)
    if n > 4:
        iv = np.concatenate([np.linspace(0.17, 0.075, n - 4), np.linspace(0.085, 0.16, 4)])
    else:
        iv = np.full(n, 0.16)
    starts, t = [], t0
    for v in iv:
        starts.append(t)
        t += float(v)
    riffle_end = t

    wall = _on_wall(deck)
    deal_stag, deal_t, ENTER = 0.07, 0.5, 0.10
    deal_start = riffle_end + 0.18
    deal_at = {it["key"]: deal_start + i * deal_stag for i, it in enumerate(wall)}
    total = deal_start + max(0, len(wall) - 1) * deal_stag + deal_t + 1.5
    title = _title_layer(name, message)

    for fi in range(int(total * FPS)):
        t = fi / FPS
        canvas = Image.new("RGBA", (W, H), BG + (255,))

        landed = [k for k, s in enumerate(starts) if t >= s + ENTER]
        top = landed[-1] if landed else None
        if top is not None:
            for k in range(max(0, top - 4), top + 1):      # a few pages of thickness
                it = deck[k]
                if t >= deal_at.get(it["key"], float("inf")):
                    continue
                rot, dx, dy = jit[it["key"]]
                depth = top - k
                comp.place(canvas, cards[it["key"]], HERO * card_ratio,
                           W / 2 + dx * 0.25 - depth * 3, H / 2 + dy * 0.25 + depth * 2,
                           rot * 0.6 - depth * 0.8, key=("fb", it["key"]))

        for k, s in enumerate(starts):                      # the page flicking in
            dt = t - s
            if dt < 0 or dt >= ENTER:
                continue
            it = deck[k]
            u = _ease_out(dt / ENTER)
            rot, dx, dy = jit[it["key"]]
            comp.place(canvas, cards[it["key"]], HERO * card_ratio * (0.86 + 0.14 * u),
                       W / 2 + 110 * (1 - u) + dx * 0.25 * u,
                       H / 2 - 70 * (1 - u) + dy * 0.25 * u,
                       9 * (1 - u) + rot * 0.6 * u,
                       alpha=min(1.0, 0.45 + u), blur=5 * (1 - u), key=("fb", it["key"]))

        for it in wall:                                     # dealt out to the wall
            dt = t - deal_at[it["key"]]
            if dt < 0:
                continue
            u = _ease_io(min(1.0, dt / deal_t))
            rot0, dx0, dy0 = jit[it["key"]]
            start_scale = HERO * card_ratio
            comp.place(canvas, cards[it["key"]],
                       start_scale + (it["card_scale"] - start_scale) * u,
                       (W / 2 + dx0 * 0.25) + (it["cx"] - W / 2 - dx0 * 0.25) * u,
                       (H / 2 + dy0 * 0.25) + (it["cy"] - H / 2 - dy0 * 0.25) * u,
                       rot0 * 0.6 + (it["rot"] - rot0 * 0.6) * u, key=("fb", it["key"]))

        frame = _finish(canvas)
        if t < t0 + 0.45:
            frame = _overlay_title(comp, frame, title, t, title_in, t0, 0.45)
        yield frame


def _overlay_title(comp, frame, title, t, fade_in, blow_at, blow_len):
    """The name holds on black until the first arrival blows it away."""
    if t < fade_in:
        alpha, scale, blur = _ease_out(t / fade_in), 1.0, 0.0
    elif t < blow_at:
        alpha, scale, blur = 1.0, 1.0, 0.0
    else:
        u = (t - blow_at) / blow_len
        alpha, scale, blur = (1 - u) ** 2, 1 + 0.9 * u, 9 * u
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    comp.place(lay, title, scale, W / 2, H / 2, 0.0, alpha=alpha, blur=blur,
               shadow=False, key="title")
    return Image.alpha_composite(frame.convert("RGBA"), lay).convert("RGB")


def render_outro_tail(items: list, name: str, dates: str = "") -> Iterator[Image.Image]:
    """Where the rewind lands: the wall holds, dims, and the name comes up."""
    comp = _Compositor()
    hold, dim_t, card_t, fade = 1.3, 1.1, 4.0, 1.5
    total = hold + dim_t + card_t + fade
    end = _text_layer([
        (name, _font(88, True), CREAM, H / 2 - (96 if dates else 44)),
        (dates, _font(44), GOLD, H / 2 + 36),
    ])
    cache: dict = {}
    for fi in range(int(total * FPS)):
        t = fi / FPS
        if t < hold:
            dim, alpha = 0.0, 0.0
        elif t < hold + dim_t:
            dim, alpha = 0.64 * _ease_io((t - hold) / dim_t), 0.0
        elif t < hold + dim_t + card_t:
            dim, alpha = 0.64, _ease_out(min(1.0, (t - hold - dim_t) / 1.2))
        else:
            u = (t - hold - dim_t - card_t) / fade
            dim, alpha = 0.64 + 0.36 * u, 1.0 - u
        key = round(dim, 3)
        if key not in cache:
            cache[key] = wall_image(items, comp, dim=key).convert("RGBA")
        frame = cache[key].copy()
        if alpha > 0:
            lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            comp.place(lay, end, 1.0, W / 2, H / 2, 0.0, alpha=alpha, shadow=False, key="end")
            frame = Image.alpha_composite(frame, lay)
        yield frame.convert("RGB")
