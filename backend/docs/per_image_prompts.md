# Per-Image Motion Prompts (Bedrock)

## What this is

Each uploaded photo is sent to AWS Bedrock (Claude Haiku 4.5) along with a
"shot director" system prompt that encodes Runway's documented prompting
rules. The model returns a tailored motion prompt for that specific image,
which is then passed to Runway as `promptText` for image-to-video generation.

Replaces the previous single static prompt. Output should be noticeably
better — particularly the motion quality on faces, the camera direction
matching the photo type, and the absence of the things Runway docs say
degrade output (negative phrasing, image-description, conceptual adjectives).

## Files changed

- `src/shared/prompt_generator.py` — **new**. Bedrock client, system prompt,
  fallback prompt, image resize helper.
- `src/functions/video_generator/handler.py` — calls `generate_motion_prompt`
  per file, persists the result to `OrderFile.runway_prompt` (existing field).
- `template.yaml` — adds `bedrock:InvokeModel` to the shared Lambda role,
  bumps `VideoGeneratorFunction` timeout to 180s and memory to 1024MB,
  bumps `VideoGenerationQueue` visibility timeout to 1200s, adds env vars
  for the kill-switch and model ID.

No changes needed to `db.py` — `update_file_status(**extra_fields)` already
supports arbitrary fields, so passing `runway_prompt=prompt` just works.

## Before you deploy

1. **Enable Bedrock model access** in the AWS console:
   - eu-west-1 → Bedrock → "Model access" → request access to
     **Anthropic / Claude Haiku 4.5** (and any version newer than 4.5 if you
     want to override later).
   - Approval is usually instant for Anthropic models.
2. **Verify the inference profile exists**:
   ```
   aws bedrock list-inference-profiles --region eu-west-1 \
     | grep -A2 claude-haiku
   ```
   You should see `eu.anthropic.claude-haiku-4-5-20251001-v1:0` (or similar).
   If not, set `BEDROCK_PROMPT_MODEL` to a direct foundation-model ID instead
   (e.g. `anthropic.claude-haiku-4-5-20251001-v1:0`) in `template.yaml` and
   adjust the IAM resource ARN accordingly.

## Environment variables on VideoGeneratorFunction

| Var | Default | Purpose |
|---|---|---|
| `USE_PER_IMAGE_PROMPTS` | `"true"` | Kill switch. Set to `"false"` to revert to the static `FALLBACK_PROMPT` for every file (no Bedrock call). |
| `BEDROCK_PROMPT_MODEL` | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` | Model used to write prompts. Swap for Sonnet if Haiku quality isn't good enough. |
| `BEDROCK_REGION` | `eu-west-1` (from `AWS::Region`) | Bedrock region. |

## Testing

The cheapest end-to-end test is a one-stone dev order:

```bash
cd backend
./scripts/test_video_pipeline.py --order-id <some-test-order-id>
```

To eyeball just the prompt generation without running the whole pipeline,
invoke the function locally with a sample SQS event from `events/`:

```bash
sam local invoke VideoGeneratorFunction \
  -e events/video_generation_queue.json \
  --env-vars env.local.json
```

Check CloudWatch logs for lines like:

```
File <id> submitted to Runway: task <tid> (prompt: The camera slowly pushes in...)
```

The full generated prompt is now stored on the file record (`runway_prompt`
field in DynamoDB) so you can inspect what was sent for any past order:

```bash
aws dynamodb get-item \
  --table-name memories-orders-dev \
  --key '{"PK":{"S":"ORDER#<order_id>"},"SK":{"S":"FILE#<file_id>"}}' \
  --projection-expression "runway_prompt,caption,original_filename"
```

## Cost

Per image: ~1500 input image tokens + ~500 system tokens + ~80 output tokens
on Haiku 4.5. Roughly **$0.003 per image**, or **~$0.04 per typical
3-stone / 12-photo order**. Against a €99 sale this is negligible.

## Failure behavior

The Bedrock call is wrapped in try/except. On any failure (model unavailable,
oversized image, invalid response, IAM problem) the file falls back to a
safe generic motion prompt (`FALLBACK_PROMPT` in `prompt_generator.py`) and
the pipeline continues. Failures are logged but not raised. **The video
pipeline never breaks because of this enhancement.**

## A note on Kling / model swapping

This layer is provider-agnostic — the same generated prompt would work
fine for Kling, Veo, or any image-to-video model. If you decide to test
Kling, swap the API call in `_submit_to_runway` (or split it behind a
provider abstraction) and reuse `generate_motion_prompt` unchanged.

## Iterating on the system prompt

The "shot director" system prompt lives in `SYSTEM_PROMPT` in
`src/shared/prompt_generator.py`. If output quality needs tuning:

- Add more shot-type examples to the EXAMPLES section.
- Tighten or loosen the speed/style constraints.
- Add per-shot-type word lists if Haiku keeps drifting back to conceptual
  language.

Iteration is cheap — no infra changes, just edit and redeploy.
