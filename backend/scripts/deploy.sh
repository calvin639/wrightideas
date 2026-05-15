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

# Use whichever Stripe key var is set
STRIPE_KEY="${STRIPE_SECRET_KEY:-$STRIPE_SANDBOX_KEY}"

# Webhook secret can be placeholder on first deploy
WEBHOOK_SECRET="${STRIPE_WEBHOOK_SECRET:-whsec_placeholder_update_after_deploy}"

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
if [ "$ENV" = "prod" ]; then
  DEFAULT_MODEL="gen4.5"
else
  DEFAULT_MODEL="gen3a_turbo"
fi

MODEL="${RUNWAY_MODEL:-$DEFAULT_MODEL}"

echo "🚀 Deploying to environment: $ENV"
echo "   Stripe key:   ${STRIPE_KEY:0:10}..."
echo "   Runway key:   ${RUNWAY_AI_KEY:0:10}..."
echo "   Webhook:      ${WEBHOOK_SECRET:0:15}..."
echo "   Runway URL:   $RUNWAY_URL"
echo "   Runway model: $MODEL"
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
)

sam deploy \
  --config-env "$ENV" \
  --parameter-overrides "${OVERRIDES[@]}" \
  2>&1

echo ""
echo "✅ Deploy complete (env=$ENV model=$MODEL)"
