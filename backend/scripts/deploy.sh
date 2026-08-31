#!/bin/bash
# Deploy the Memories in Stone backend.
# Reads API keys from environment (sourced from ~/.bashrc).
#
# Usage:
#   ./scripts/deploy.sh [dev|prod]
#
# Model override (dev only — prod always uses gen4.5):
#   RUNWAY_MODEL=gen4.5 ./scripts/deploy.sh dev

set -e

ENV=${1:-dev}

# Source .bashrc to pick up STRIPE_SANDBOX_KEY, RUNWAY_AI_KEY etc.
if [ -f ~/.bashrc ]; then
  source ~/.bashrc
fi

# ── Validate secrets are present ─────────────────────────────────────────────
if [ -z "$STRIPE_SANDBOX_KEY" ] && [ -z "$STRIPE_SECRET_KEY" ]; then
  echo "❌ Missing STRIPE_SANDBOX_KEY (or STRIPE_SECRET_KEY) in environment"
  exit 1
fi
if [ -z "$RUNWAY_AI_KEY" ]; then
  echo "❌ Missing RUNWAY_AI_KEY in environment"
  exit 1
fi

# fal.ai is the video generation provider. Accept either name — fal's own
# convention is FAL_KEY, the shell profile here defines FAL_AI_API_KEY.
FAL_KEY_VALUE="${FAL_KEY:-$FAL_AI_API_KEY}"
if [ -z "$FAL_KEY_VALUE" ]; then
  echo "❌ Missing FAL_KEY / FAL_AI_API_KEY in environment"
  echo "   Video generation runs on fal.ai — see backend/RUNBOOK.md"
  exit 1
fi

# Use whichever Stripe key var is set
STRIPE_KEY="${STRIPE_SECRET_KEY:-$STRIPE_SANDBOX_KEY}"

# Webhook secret — prefer env-specific name (STRIPE_WH_DEV / STRIPE_WH_PROD),
# fall back to generic STRIPE_WEBHOOK_SECRET, then placeholder on first deploy.
if [ "$ENV" = "prod" ]; then
  WEBHOOK_VAR="STRIPE_WH_PROD"
  WEBHOOK_SECRET="${STRIPE_WH_PROD:-${STRIPE_WEBHOOK_SECRET:-whsec_placeholder_update_after_deploy}}"
else
  WEBHOOK_VAR="STRIPE_WH_DEV"
  WEBHOOK_SECRET="${STRIPE_WH_DEV:-${STRIPE_WEBHOOK_SECRET:-whsec_placeholder_update_after_deploy}}"
fi

# Warn loudly if we're about to deploy the placeholder
case "$WEBHOOK_SECRET" in
  whsec_placeholder*)
    echo "⚠️  $WEBHOOK_VAR / STRIPE_WEBHOOK_SECRET is unset — deploying placeholder."
    echo "    The Stripe webhook handler will return 500 until this is fixed."
    ;;
esac

# Runway webhook URL: prefer env var, then derive from live stack output, then placeholder
if [ -n "$RUNWAY_WEBHOOK_URL" ]; then
  RUNWAY_URL="$RUNWAY_WEBHOOK_URL"
else
  STACK_API_URL=$(aws cloudformation describe-stacks \
    --stack-name "memories-in-stone-$ENV" \
    --region eu-west-1 \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
    --output text 2>/dev/null || true)
  if [ -n "$STACK_API_URL" ] && [ "$STACK_API_URL" != "None" ]; then
    RUNWAY_URL="${STACK_API_URL}/webhooks/runway"
  else
    RUNWAY_URL="https://placeholder/webhooks/runway"
  fi
fi

# ── Environment-specific settings ────────────────────────────────────────────
# seedance2 in both environments: it is the model documented to accept the
# first/last keyframe array, which is what stops the subject drifting across
# the clip. Overriding this to a model without keyframe support silently loses
# that guarantee — see KEYFRAME_MODELS in video_generator.
DEFAULT_MODEL="seedance2"
MODEL="${RUNWAY_MODEL:-$DEFAULT_MODEL}"

# fal model slug. seedance is the only tested model that accepts an end
# keyframe AND still animates when start and end are identical — Kling freezes.
VIDEO_MODEL="${VIDEO_MODEL:-bytedance/seedance-2.0/image-to-video}"

# ── ECR (for the image_prep container function) ──────────────────────────────
# ImagePrepFunction ships as a container image because it carries torch for
# Real-ESRGAN and GFPGAN. SAM needs a repository to push to, and an
# authenticated Docker client to push with.
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="memories-image-prep-${ENV}"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION:-eu-west-1}.amazonaws.com/${ECR_REPO}"
REGION="${AWS_REGION:-eu-west-1}"

if ! docker info >/dev/null 2>&1; then
  echo "❌ Docker daemon is not running — start Docker Desktop and retry."
  echo "   The image_prep function is a container image and cannot be built without it."
  exit 1
fi

# Create on first deploy; harmless on every deploy after.
if ! aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" >/dev/null 2>&1; then
  echo "📦 Creating ECR repository $ECR_REPO"
  aws ecr create-repository \
    --repository-name "$ECR_REPO" \
    --region "$REGION" \
    --image-scanning-configuration scanOnPush=true \
    >/dev/null

  # Without this the repo accumulates every image ever built, at $0.10/GB/month
  # for images around 3GB each.
  aws ecr put-lifecycle-policy \
    --repository-name "$ECR_REPO" \
    --region "$REGION" \
    --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"Keep last 3 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":3},"action":{"type":"expire"}}]}' \
    >/dev/null
fi

echo "🔑 Authenticating Docker to ECR"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "🚀 Deploying to environment: $ENV"
echo "   Stripe key:   ${STRIPE_KEY:0:10}..."
echo "   Runway key:   ${RUNWAY_AI_KEY:0:10}..."
echo "   Webhook:      ${WEBHOOK_SECRET:0:15}..."
echo "   Runway URL:   $RUNWAY_URL"
echo "   Runway model: $MODEL"
echo "   fal key:      ${FAL_KEY_VALUE:0:10}..."
echo "   fal model:    $VIDEO_MODEL"
echo ""

# NOTE: --parameter-overrides on the CLI replaces all samconfig.toml overrides,
# so every non-default parameter must be listed here.
OVERRIDES=(
  "Environment=$ENV"
  "FrontendUrl=https://memories.wrightideas.biz"
  "SesFromEmail=noreply@wrightideas.biz"
  "StripeSecretKey=$STRIPE_KEY"
  "RunwayApiKey=$RUNWAY_AI_KEY"
  "StripeWebhookSecret=$WEBHOOK_SECRET"
  "RunwayWebhookUrl=$RUNWAY_URL"
  "RunwayModel=$MODEL"
  "FalApiKey=$FAL_KEY_VALUE"
  "VideoModel=$VIDEO_MODEL"
)

# Build before deploying. `sam deploy` reads .aws-sam/build/template.yaml, not
# template.yaml — without this, running the script directly (rather than via
# `make deploy`, which builds first) silently deploys whatever was last built
# and template edits appear to have no effect.
echo "🔨 Building"
sam build --cached --parallel

sam deploy \
  --config-env "$ENV" \
  --parameter-overrides "${OVERRIDES[@]}" \
  --image-repository "$ECR_URI" \
  2>&1

echo ""
echo "✅ Deploy complete (env=$ENV model=$MODEL)"
