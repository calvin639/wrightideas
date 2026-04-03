#!/bin/bash
# Deploy the Memories in Stone backend.
# Reads API keys from environment (sourced from ~/.bashrc).
# Usage: ./scripts/deploy.sh [dev|prod]

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

# Webhook secret + Runway URL can be placeholders on first deploy
WEBHOOK_SECRET="${STRIPE_WEBHOOK_SECRET:-whsec_placeholder_update_after_deploy}"
RUNWAY_URL="${RUNWAY_WEBHOOK_URL:-https://placeholder/webhooks/runway}"

echo "🚀 Deploying to environment: $ENV"
echo "   Stripe key:   ${STRIPE_KEY:0:10}..."
echo "   Runway key:   ${RUNWAY_AI_KEY:0:10}..."
echo "   Webhook:      ${WEBHOOK_SECRET:0:15}..."
echo ""

sam deploy \
  --config-env "$ENV" \
  --parameter-overrides \
    "StripeSecretKey=$STRIPE_KEY" \
    "RunwayApiKey=$RUNWAY_AI_KEY" \
    "StripeWebhookSecret=$WEBHOOK_SECRET" \
    "RunwayWebhookUrl=$RUNWAY_URL" \
  2>&1

echo ""
echo "✅ Deploy complete"
