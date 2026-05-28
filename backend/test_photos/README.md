# Test photos

Drop photos here for `scripts/test_video_pipeline.py` to upload as part of a
test order. The script discovers every supported file in this folder, in
filename order, and uploads each one via its presigned S3 URL — exactly the
same flow a real customer goes through.

**Supported extensions** (must match `ALLOWED_TYPES` in
`src/functions/create_order/handler.py`):

- Images: `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`
- Videos: `.mp4`, `.mov`

**Heads-up on cost:** each file becomes one Runway clip when the pipeline
runs, which is a real per-clip charge against your Runway API key. Keep this
folder modest (3–5 files) unless you specifically want to stress-test the
montage builder.

The actual photos are git-ignored — only this README is tracked. Use any
photos you like; they don't need to live anywhere else in the repo.

If you want to keep photos elsewhere on disk, point the script at them with:

```bash
TEST_PHOTOS_DIR=/some/other/folder python3 scripts/test_video_pipeline.py
```
