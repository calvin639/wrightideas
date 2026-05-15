#!/bin/bash
# Patch a Memories-in-Stone order's video_url to point at the CloudFront
# distribution instead of the (blocked) direct S3 URL.
#
# Usage:
#   ./scripts/fix_video_url.sh [order_id] [env]
#
# Defaults:
#   env       = dev
#   order_id  = most recent COMPLETE order in that env
#
# Examples:
#   ./scripts/fix_video_url.sh                       # newest COMPLETE order in dev
#   ./scripts/fix_video_url.sh abc-123-def           # specific order in dev
#   ./scripts/fix_video_url.sh abc-123-def prod      # specific order in prod

set -e

ORDER_ID="${1:-}"
ENV="${2:-dev}"
REGION="eu-west-1"
STACK="memories-in-stone-${ENV}"
TABLE="memories-orders-${ENV}"

echo "▶ Environment: $ENV  (stack: $STACK, table: $TABLE)"

# ── 1. Get the CloudFront URL from the stack outputs ─────────────────────────
CF_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`VideosCloudfrontUrl`].OutputValue' \
  --output text)

if [ -z "$CF_URL" ] || [ "$CF_URL" = "None" ]; then
  echo "❌ Could not read VideosCloudfrontUrl from stack $STACK"
  exit 1
fi
echo "▶ CloudFront URL: $CF_URL"

# ── 2. Resolve the order_id ──────────────────────────────────────────────────
if [ -z "$ORDER_ID" ]; then
  echo "▶ No order_id given — scanning table for most recent order with a video_url…"
  # Scan rather than GSI-query: GSI only includes rows where GSI1SK is also set,
  # and update_order_status doesn't touch GSI1SK so coverage is unreliable.
  # Table is tiny so a full scan is fine.
  ORDER_ID=$(aws dynamodb scan \
    --table-name "$TABLE" \
    --filter-expression "SK = :sk AND attribute_exists(video_url) AND video_url <> :empty" \
    --expression-attribute-values '{":sk":{"S":"METADATA"},":empty":{"S":""}}' \
    --region "$REGION" \
    --output json \
  | python3 -c "
import json, sys
items = json.load(sys.stdin).get('Items', [])
if not items:
    sys.exit(0)
items.sort(key=lambda i: i.get('created_at', {}).get('S', ''), reverse=True)
print(items[0]['order_id']['S'])
")
fi

if [ -z "$ORDER_ID" ] || [ "$ORDER_ID" = "None" ]; then
  echo "❌ Couldn't auto-detect an order_id"
  echo "   Pass one explicitly:  ./scripts/fix_video_url.sh <order_id> [env]"
  exit 1
fi
echo "▶ Order: $ORDER_ID"

# ── 3. Show what's currently stored ──────────────────────────────────────────
CURRENT=$(aws dynamodb get-item \
  --table-name "$TABLE" \
  --key "{\"PK\":{\"S\":\"ORDER#$ORDER_ID\"},\"SK\":{\"S\":\"METADATA\"}}" \
  --region "$REGION" \
  --query 'Item.video_url.S' \
  --output text)
echo "▶ Current video_url: $CURRENT"

# ── 4. Build the new URL ─────────────────────────────────────────────────────
VIDEO_KEY="tributes/$ORDER_ID/memorial.mp4"
NEW_URL="${CF_URL%/}/$VIDEO_KEY"
echo "▶ New video_url:     $NEW_URL"

if [ "$CURRENT" = "$NEW_URL" ]; then
  echo "✓ Already correct — nothing to do"
  exit 0
fi

# ── 5. Verify the video actually exists at that CloudFront path ──────────────
echo "▶ Checking $NEW_URL is reachable…"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -I "$NEW_URL")
if [ "$STATUS" != "200" ]; then
  echo "❌ CloudFront returned HTTP $STATUS for $NEW_URL"
  echo "   The video file may not be in S3 at $VIDEO_KEY — check the bucket before patching."
  exit 1
fi
echo "✓ HTTP $STATUS — file is reachable"

# ── 6. Patch DynamoDB ────────────────────────────────────────────────────────
read -p "Update DynamoDB row? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted — no changes made"
  exit 0
fi

aws dynamodb update-item \
  --table-name "$TABLE" \
  --key "{\"PK\":{\"S\":\"ORDER#$ORDER_ID\"},\"SK\":{\"S\":\"METADATA\"}}" \
  --update-expression "SET video_url = :u" \
  --expression-attribute-values "{\":u\":{\"S\":\"$NEW_URL\"}}" \
  --region "$REGION" \
  --return-values UPDATED_NEW \
  --output table

echo ""
echo "✅ Patched. Reload the tribute page to verify."
