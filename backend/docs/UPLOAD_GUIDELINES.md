# Getting the best from your photos

Customer-facing copy for the upload flow. Each tip maps to an automatic
check in the pipeline (`image_prep._quality_warnings`). Tone: warm, brief,
never blaming.

> **Status note:** the checks currently run inside ImagePrepFunction — i.e.
> after payment — and surface only in the admin review email. Showing them to
> the customer at upload time (the intent of this document) needs a
> lightweight pre-payment assess endpoint plus frontend work; until that
> exists, treat the per-tip flags below as where the UI *will* hook in, and
> publish the tips themselves as static guidance on the upload page — they
> are useful even with no automation behind them.

---

## The one-line version

**The better the photo going in, the more alive the memory coming out.**

## Scan or original file beats a photo of a photo

If your photo lives in an album or a frame, the best result comes from a
scan — most phones can do this well (use a scanning app or your phone's
built-in document scanner in photo mode). Taking a picture *of* the picture
usually adds glare, reflections and a texture that our enhancement can't fully
undo.

*If you must photograph a print:* take it out of the frame or album sleeve,
lay it flat near a bright window, stand directly above it, and make sure no
lamp or window is reflected in the surface.

→ pipeline flag: `possible_glare`, `possible_rephotographed_print_or_screen`

## Don't photograph a screen

A photo of a computer or phone screen picks up the screen's pixel grid, which
shows up as shimmering patterns in the video. If the photo exists digitally,
upload the file itself — from your camera roll, an email attachment, or a
download — rather than photographing it displayed somewhere.

→ pipeline flag: `possible_rephotographed_print_or_screen`

## Use the largest version you have

A photo saved from a chat app or social media is often a small, compressed
copy. Look for the original: your camera roll, the person who first shared
it, or the original scan. As a guide, anything under about 1000 pixels on the
long edge will noticeably limit the final quality.

→ pipeline flag: `low_resolution`

## Faces matter most

Choose photos where your loved one's face is clearly visible and reasonably
large in the frame. Our enhancement is careful with faces — the smaller a face
is in the photo, the more gently it is treated, so a sharp close-up will always
come out better than a distant group shot. Group photos are welcome; just know
the closer shots will shine brightest.

## Damaged photos are okay

Creases, fading, colour casts and small scratches are normal for treasured
photos — the pipeline corrects tone and fading automatically and treats
black-and-white and sepia photos with their character intact. You don't need
to repair anything before uploading.

## What happens after upload

Every photo is individually assessed and prepared: oriented, framed for video,
tone-corrected, and — where it genuinely helps — carefully restored. A human
reviews the prepared photos before any video is generated.
