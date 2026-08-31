# Running the Memories in Stone pipeline end to end

How to put real photos through the whole system — order, payment, image prep,
clip generation on fal.ai, montage, delivery email — and how to tell what happened.

Read `CLAUDE.md` first for what the project is. This file is only about running it.

> **This costs real money.** Every photo becomes one fal.ai clip on your live
> API key. Keep test batches to 3–5 photos.

---

## 1. Prerequisites

On the machine you are running from:

```bash
aws sts get-caller-identity          # must return account 022761975017
aws configure get region             # or export AWS_REGION=eu-west-1
python3 -c "import boto3, requests"  # pip3 install boto3 requests
```

These environment variables must be set (the deploy script sources `~/.bashrc`):

| Variable | Used for |
|---|---|
| `FAL_AI_API_KEY` | fal.ai API — the video generation provider (`FAL_KEY` also accepted) |
| `RUNWAY_AI_KEY` | DEPRECATED — retained so the stack deploys; nothing reads it |
| `STRIPE_SANDBOX_KEY` *or* `STRIPE_SECRET_KEY` | Stripe checkout |
| `STRIPE_WH_DEV` | Stripe webhook signature verification |

**You only need Docker if you intend to deploy.** Running the pipeline against
the already-deployed dev stack needs nothing but the AWS CLI and Python.

The dev frontend is behind HTTP basic auth. Credentials are in the project
memory note *Dev preview gate* — the browser will prompt during checkout.

---

## 2. Run it

```bash
cd backend
mkdir -p test_photos          # drop 3-5 JPEGs in here
python3 scripts/test_video_pipeline.py --music nature
```

The script creates the order, uploads each photo to its presigned URL exactly as
the frontend does, opens a real Stripe test-mode checkout, and prints the order
ID. **Complete the payment in the browser** — nothing runs until Stripe's
webhook fires.

Test card: `4242 4242 4242 4242`, any future expiry, any CVC.

Useful flags:

```bash
--music none|beautiful|emotion|nature    # background track
--model seedance2                        # legacy Runway flag; see VIDEO_MODEL below
TEST_PHOTOS_DIR=/path/to/photos          # use a different folder
```

**Provider: fal.ai**, model `bytedance/seedance-2.0/image-to-video`, set via the
`VideoModel` template parameter (override at deploy with `VIDEO_MODEL=...`).

Seedance is the default because it is the only tested model that both accepts an
end keyframe AND still animates when the end frame is identical to the start.
Kling v3 Pro and O3 Pro accept the end frame and freeze solid regardless of
prompt (measured: max drift 1.06/255 over 5s). `fal-ai/vidu/q3/image-to-video`
works and is the fallback. Anything outside `KEYFRAME_MODELS` in
`video_generator` silently loses identity pinning.

---

## 3. Watch it

```bash
./scripts/check_pipeline.sh <order_id>
```

One command, and the thing to reach for first. It reports the order and per-file
status from DynamoDB, S3 objects at each stage, the Step Functions execution,
recent Lambda errors, and the tribute URL.

For live progress, the execution history in the console is clearer than logs —
it shows exactly which file is in which state:

```
https://eu-west-1.console.aws.amazon.com/states/home?region=eu-west-1#/statemachines
```

Executions are named `order-<order_id>`.

Expect **5–15 minutes** for a 3–5 photo order. Clip generation is the slow part.

---

## 4. What should happen

```
create order      → PENDING_UPLOAD   files UPLOADED
photos uploaded   → PENDING_PAYMENT
payment           → PAID             confirmation email sent
                                     Step Functions execution starts
PrepareFiles      → PROCESSING       files PREPARED
                                     s3://…-uploads/prepared/<order>/<file>.jpg
GenerateClips     →                  files DONE
                                     s3://…-videos/clips/<order>/<file>.mp4
BuildMontage      → COMPLETE         s3://…-videos/tributes/<order>/memorial.mp4
                                     "video ready" email sent
```

Two emails should arrive: order confirmation on payment, and video ready on
completion.

Videos the customer uploads are **not** sent to fal. They go straight to the
montage, trimmed to 5 seconds from the chosen start point, with audio muted.
Their file status goes to `SKIPPED`, which is expected and not an error.

---

## 5. When it goes wrong

**Start with the Step Functions execution**, not the logs. It tells you which
stage failed and which file, and the logs then tell you why.

```bash
aws logs tail /aws/lambda/memories-image-prep-dev --region eu-west-1 --follow
aws logs tail /aws/lambda/memories-video-generator-dev --region eu-west-1 --follow
aws logs tail /aws/lambda/memories-montage-builder-dev --region eu-west-1 --follow
```

| Symptom | Cause |
|---|---|
| No execution exists | Payment never completed, or the Stripe webhook failed. Check `memories-stripe-webhook-dev` logs. |
| `restoration needs 4096MB` | Expected until the Lambda memory quota increase lands. Photos still get deterministic processing. See §6. |
| fal error mentioning `end_image_url` | The model does not accept an end keyframe. Check `VideoModel` is in `KEYFRAME_MODELS` — removing it drops identity pinning. |
| `ImageDownloadFailure` / "Failed to download the file" | The presigned S3 URL expired before fal fetched it. fal downloads server-side and may queue first, so `S3_PRESIGN_EXPIRY` must outlive the queue wait. Re-sign, never reuse. |
| Clips generate but are **frozen** | The prompt described a one-way journey. The same frame is pinned first+last, so the prompt must resolve back to rest — see RULE 0 in `prompt_generator.SYSTEM_PROMPT`. |
| `Output audio has sensitive content` | Seedance defaults `generate_audio` on and screens the audio it invents. We force it off; if this appears, that flag regressed. |
| A file is `FAILED`, order still completes | By design — up to 60% of files may fail and the montage still runs with the rest. |
| Order stuck in `PROCESSING` | Look at the execution. The Wait/Poll loop polls fal; a genuinely stuck request shows there. |

The `/webhooks/runway` endpoint is **deprecated and inert** — Runway is no longer
the provider, and even before that it only observed. If a clip went missing, the
answer is in the Step Functions execution history.

---

## 6. Enabling photo restoration

Currently degraded: generative restoration (GFPGAN + Real-ESRGAN) needs ~3.8GB
and the account's Lambda memory quota is the legacy 3008MB, so it is skipped and
photos get deterministic processing only.

Once the quota increase to 10240MB is approved (Service Quotas, eu-west-1,
`lambda` / `L-B99A9384`):

```bash
# backend/template.yaml → ImagePrepFunction
MemorySize: 3008   →   10240
```

Then `./scripts/deploy.sh dev`. No other change — the guard reads the live
function memory and enables itself. Confirm with:

```bash
aws logs tail /aws/lambda/memories-image-prep-dev --region eu-west-1 --since 10m \
  | grep -E "Restoring|restored="
```

---

## 7. Deploying changes

```bash
cd backend
./scripts/deploy.sh dev
```

Needs Docker running — `ImagePrepFunction` is a container image. The script
builds, creates/authenticates ECR, pushes, and deploys. First push is ~4GB;
later ones send only changed layers.

The frontend deploys separately, on push to `main`, via GitHub Actions.

**If a deploy fails mid-way**, wait for the stack to reach a stable state before
retrying — a second attempt during `UPDATE_ROLLBACK_IN_PROGRESS` fails
confusingly:

```bash
aws cloudformation describe-stacks --stack-name memories-in-stone-dev \
  --region eu-west-1 --query 'Stacks[0].StackStatus' --output text
```

---

## 8. Verifying without spending money

To exercise image prep alone — no Stripe, no clip generation:

```bash
# 1. Create an order (returns order_id + presigned upload URL)
curl -s -X POST https://621yr9lwh0.execute-api.eu-west-1.amazonaws.com/dev/orders \
  -H 'Content-Type: application/json' \
  -d '{"customer_name":"Test","customer_email":"you@example.com",
       "loved_one_name":"Test","stone_message":"Test","stone_style":"black_slate",
       "stone_quantity":1,"music_choice":"none",
       "files":[{"filename":"photo.jpg","content_type":"image/jpeg",
                 "crop_rect":{"x":0.1,"y":0.0,"w":0.8,"h":0.45}}]}'

# 2. PUT the photo to the returned upload_url with -H 'Content-Type: image/jpeg'

# 3. Invoke prep directly
aws lambda invoke --function-name memories-image-prep-dev --region eu-west-1 \
  --cli-read-timeout 900 \
  --payload "$(echo -n '{"order_id":"<id>","file_id":"<id>"}' | base64)" /tmp/out.json
cat /tmp/out.json
```

The prepared 1280x720 frame lands at `prepared/<order>/<file>.jpg`, with the
unenhanced `_before.jpg` beside it for comparison.

To rerun prep on a file, reset its status first — prep is idempotent and will
otherwise skip:

```bash
aws dynamodb update-item --table-name memories-orders-dev --region eu-west-1 \
  --key '{"PK":{"S":"ORDER#<order>"},"SK":{"S":"FILE#<file>"}}' \
  --update-expression "SET #s = :v" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":v":{"S":"UPLOADED"}}'
```
