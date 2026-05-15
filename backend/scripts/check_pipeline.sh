#!/usr/bin/env bash
# check_pipeline.sh — verify a Memories in Stone test pipeline run
#
# Usage:
#   ./check_pipeline.sh                  # find and check the most recent order
#   ./check_pipeline.sh <order_id>       # check a specific order
#
# Reads from the dev stack (memories-in-stone-dev / eu-west-1).

set -u

STACK_NAME="memories-in-stone-dev"
REGION="eu-west-1"
ARG_ORDER_ID="${1:-}"

# ── Colors (only when stdout is a tty) ────────────────────────────────────────
if [[ -t 1 ]]; then
  G="\033[32m"; R="\033[31m"; Y="\033[33m"; B="\033[34m"; D="\033[2m"; N="\033[0m"
else
  G=""; R=""; Y=""; B=""; D=""; N=""
fi
ok()   { printf "${G}PASS${N}  %s\n" "$1"; }
bad()  { printf "${R}FAIL${N}  %s\n" "$1"; }
warn() { printf "${Y}WARN${N}  %s\n" "$1"; }
hdr()  { printf "\n${B}── %s ──${N}\n" "$1"; }
note() { printf "${D}      %s${N}\n" "$1"; }

# ── 0. Sanity ─────────────────────────────────────────────────────────────────
hdr "Environment"
if ! command -v aws >/dev/null; then
  bad "aws CLI not on PATH"; exit 1
fi
WHO=$(aws sts get-caller-identity --region "$REGION" --output json 2>/dev/null) || {
  bad "aws credentials not configured for $REGION"; exit 1;
}
echo "$WHO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'      account={d[\"Account\"]}  arn={d[\"Arn\"]}')"
ok "AWS CLI + credentials"

# ── 1. Pull stack outputs ─────────────────────────────────────────────────────
hdr "CloudFormation stack: $STACK_NAME"
OUTS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs" --output json 2>/dev/null) || {
    bad "stack '$STACK_NAME' not found in $REGION"; exit 1;
}

read_out() {
  echo "$OUTS" | python3 -c "
import sys, json
o = {x['OutputKey']: x['OutputValue'] for x in json.load(sys.stdin)}
print(o.get('$1', ''))"
}

ORDERS_TABLE=$(read_out OrdersTableName)
UPLOADS_BUCKET=$(read_out UploadsBucketName)
VIDEOS_BUCKET=$(read_out VideosBucketName)
QUEUE_URL=$(read_out VideoGenerationQueueUrl)
MONTAGE_QUEUE_URL=$(read_out MontageQueueUrl)
VIDEOS_CF=$(read_out VideosCloudFrontUrl)

for k in OrdersTableName UploadsBucketName VideosBucketName VideoGenerationQueueUrl; do
  v=$(read_out "$k")
  [[ -n "$v" ]] && note "$k = $v" || warn "$k missing from stack outputs"
done
ok "stack outputs read"

# ── 2. Pick an order ──────────────────────────────────────────────────────────
hdr "Order"
if [[ -n "$ARG_ORDER_ID" ]]; then
  ORDER_ID="$ARG_ORDER_ID"
  note "using order from argument: $ORDER_ID"
else
  note "scanning $ORDERS_TABLE for the most recent order…"
  ORDER_ID=$(aws dynamodb scan \
    --table-name "$ORDERS_TABLE" --region "$REGION" \
    --filter-expression "SK = :sk" \
    --expression-attribute-values '{":sk":{"S":"METADATA"}}' \
    --projection-expression "order_id, created_at" \
    --output json 2>/dev/null \
    | python3 -c "
import sys, json
items = json.load(sys.stdin).get('Items', [])
items = [i for i in items if 'order_id' in i and 'created_at' in i]
items.sort(key=lambda i: i['created_at']['S'], reverse=True)
print(items[0]['order_id']['S'] if items else '')")
  if [[ -z "$ORDER_ID" ]]; then
    bad "no orders found in $ORDERS_TABLE"; exit 1
  fi
  note "most recent order: $ORDER_ID"
fi

# ── 3. Order metadata ─────────────────────────────────────────────────────────
hdr "Order metadata"
ORDER_JSON=$(aws dynamodb get-item \
  --table-name "$ORDERS_TABLE" --region "$REGION" \
  --key "{\"PK\":{\"S\":\"ORDER#$ORDER_ID\"},\"SK\":{\"S\":\"METADATA\"}}" \
  --output json 2>/dev/null)

if ! echo "$ORDER_JSON" | grep -q '"Item"'; then
  bad "no METADATA row for ORDER#$ORDER_ID"; exit 1
fi

eval "$(echo "$ORDER_JSON" | python3 -c "
import sys, json
item = json.load(sys.stdin)['Item']
def s(k): return item.get(k, {}).get('S', '')
def n(k): return item.get(k, {}).get('N', '')
print(f'STATUS={s(\"status\")!r}')
print(f'EMAIL={s(\"customer_email\")!r}')
print(f'CUSTOMER={s(\"customer_name\")!r}')
print(f'LOVED_ONE={s(\"loved_one_name\")!r}')
print(f'CREATED={s(\"created_at\")!r}')
print(f'UPDATED={s(\"updated_at\")!r}')
print(f'VIDEO_URL={s(\"video_url\")!r}')
print(f'QR_URL={s(\"qr_url\")!r}')
print(f'TRIBUTE_URL={s(\"tribute_url\")!r}')
print(f'STONE_QTY={n(\"stone_quantity\")!r}')
")"

note "status         = $STATUS"
note "customer_email = $EMAIL"
note "customer_name  = $CUSTOMER"
note "loved_one      = $LOVED_ONE"
note "created_at     = $CREATED"
note "updated_at     = $UPDATED"
[[ -n "$VIDEO_URL" ]]   && note "video_url      = $VIDEO_URL"
[[ -n "$QR_URL" ]]      && note "qr_url         = $QR_URL"
[[ -n "$TRIBUTE_URL" ]] && note "tribute_url    = $TRIBUTE_URL"

case "$STATUS" in
  COMPLETE) ok "order reached COMPLETE" ;;
  MONTAGE)  warn "still building montage (stuck here? FFmpeg layer present?)" ;;
  PROCESSING) warn "still generating clips with Runway (this can take minutes)" ;;
  PAID)     warn "still PAID — VideoGenerator may not have been triggered" ;;
  FAILED)   bad  "order is FAILED — see Lambda logs below" ;;
  *)        warn "unexpected status: $STATUS" ;;
esac

# ── 4. File records ───────────────────────────────────────────────────────────
hdr "OrderFile records"
FILES_JSON=$(aws dynamodb query \
  --table-name "$ORDERS_TABLE" --region "$REGION" \
  --key-condition-expression "PK = :pk AND begins_with(SK, :f)" \
  --expression-attribute-values "{\":pk\":{\"S\":\"ORDER#$ORDER_ID\"},\":f\":{\"S\":\"FILE#\"}}" \
  --output json 2>/dev/null)

echo "$FILES_JSON" | python3 - <<'PY'
import sys, json
data = json.load(sys.stdin)
items = data.get("Items", [])
if not items:
    print("      (no FILE# records — nothing was uploaded)")
    sys.exit(0)

counts = {}
for it in items:
    s = it.get("status", {}).get("S", "?")
    counts[s] = counts.get(s, 0) + 1

print(f"      total files: {len(items)}")
for s, c in sorted(counts.items()):
    print(f"        {s:<11} {c}")

# Detail any FAILED files (likely Runway error messages)
failed = [it for it in items if it.get("status", {}).get("S") == "FAILED"]
for it in failed[:5]:
    fid = it.get("file_id", {}).get("S", "?")
    err = it.get("error_message", {}).get("S", "(no error_message)")
    print(f"      FAILED  {fid}: {err[:120]}")
PY

# ── 5. S3 outputs ─────────────────────────────────────────────────────────────
hdr "S3 — uploads bucket"
UP_LS=$(aws s3 ls "s3://$UPLOADS_BUCKET/orders/$ORDER_ID/" --recursive --region "$REGION" 2>/dev/null | head -50)
if [[ -n "$UP_LS" ]]; then
  echo "$UP_LS" | sed 's/^/      /'
  ok "uploads present"
else
  bad "no objects under s3://$UPLOADS_BUCKET/orders/$ORDER_ID/"
fi

hdr "S3 — videos bucket"
VID_LS=$(aws s3 ls "s3://$VIDEOS_BUCKET/orders/$ORDER_ID/" --recursive --region "$REGION" 2>/dev/null | head -50)
if [[ -n "$VID_LS" ]]; then
  echo "$VID_LS" | sed 's/^/      /'
  if echo "$VID_LS" | grep -q '\.mp4'; then
    ok "final montage MP4 found"
  else
    warn "no .mp4 yet"
  fi
  if echo "$VID_LS" | grep -qiE '(qr|\.png|\.svg)'; then
    ok "QR code asset found"
  else
    warn "no QR asset yet"
  fi
else
  warn "no objects under s3://$VIDEOS_BUCKET/orders/$ORDER_ID/ (montage hasn't run / hasn't finished)"
fi

# ── 6. SQS queue depth (does anything look stuck?) ────────────────────────────
hdr "SQS queue depth"
for Q in "$QUEUE_URL" "$MONTAGE_QUEUE_URL"; do
  [[ -z "$Q" ]] && continue
  QNAME=$(basename "$Q")
  ATTRS=$(aws sqs get-queue-attributes --queue-url "$Q" --region "$REGION" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --output json 2>/dev/null)
  echo "$ATTRS" | python3 -c "
import sys, json
a = json.load(sys.stdin).get('Attributes', {})
v  = a.get('ApproximateNumberOfMessages', '?')
nv = a.get('ApproximateNumberOfMessagesNotVisible', '?')
print(f'      $QNAME  visible=$([ -z $v ] && echo ?){v}  in-flight=$([ -z $nv ] && echo ?){nv}')" 2>/dev/null \
  || echo "      $QNAME (attrs unavailable)"
done

# ── 7. Recent Lambda logs (errors only) ───────────────────────────────────────
hdr "Lambda logs (last 30 min, errors only)"
END_MS=$(($(date +%s) * 1000))
START_MS=$(( END_MS - 30 * 60 * 1000 ))

for FN in VideoGeneratorFunction RunwayWebhookFunction MontageBuilderFunction; do
  GROUP="/aws/lambda/$(aws cloudformation describe-stack-resource \
    --stack-name "$STACK_NAME" --region "$REGION" \
    --logical-resource-id "$FN" \
    --query 'StackResourceDetail.PhysicalResourceId' --output text 2>/dev/null)"
  if [[ "$GROUP" == "/aws/lambda/" || "$GROUP" == "/aws/lambda/None" ]]; then
    warn "$FN: log group not found"; continue
  fi
  echo ""
  echo "  $FN  ($GROUP)"
  aws logs filter-log-events \
    --log-group-name "$GROUP" \
    --region "$REGION" \
    --start-time "$START_MS" \
    --filter-pattern '?ERROR ?Error ?Exception ?Traceback ?FAILED ?Failed' \
    --max-items 20 \
    --output json 2>/dev/null \
    | python3 -c "
import sys, json
events = json.load(sys.stdin).get('events', [])
if not events:
    print('      (no errors in last 30 min)')
else:
    for e in events[-10:]:
        msg = e.get('message', '').strip().replace('\n', ' ')
        print(f'      {msg[:200]}')"
done

# ── 8. Check if this order was mentioned at all in the last 30 min ────────────
hdr "Mentions of this order in logs (last 30 min)"
FOUND_ANY=0
for FN in VideoGeneratorFunction RunwayWebhookFunction MontageBuilderFunction; do
  GROUP="/aws/lambda/$(aws cloudformation describe-stack-resource \
    --stack-name "$STACK_NAME" --region "$REGION" \
    --logical-resource-id "$FN" \
    --query 'StackResourceDetail.PhysicalResourceId' --output text 2>/dev/null)"
  HITS=$(aws logs filter-log-events \
    --log-group-name "$GROUP" --region "$REGION" \
    --start-time "$START_MS" \
    --filter-pattern "\"$ORDER_ID\"" \
    --max-items 5 --output json 2>/dev/null \
    | python3 -c "
import sys, json
events = json.load(sys.stdin).get('events', [])
print(len(events))" 2>/dev/null)
  HITS="${HITS:-0}"
  printf "      %-25s %s mentions\n" "$FN" "$HITS"
  [[ "$HITS" != "0" ]] && FOUND_ANY=1
done
[[ "$FOUND_ANY" == "1" ]] && ok "order id appears in logs" || warn "order id not seen in any Lambda log (run too long ago, or never triggered)"

# ── 9. Tribute URL ────────────────────────────────────────────────────────────
hdr "Tribute page"
note "https://memories.wrightideas.biz/tribute.html?order_id=$ORDER_ID"

echo ""
echo "Done. Order: $ORDER_ID  Status: $STATUS"
