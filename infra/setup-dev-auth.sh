#!/bin/bash
# Put the dev frontend behind HTTP Basic Auth, or take it back off.
#
#   DEV_AUTH_USER=preview DEV_AUTH_PASS='...' ./infra/setup-dev-auth.sh enable
#   ./infra/setup-dev-auth.sh disable
#   ./infra/setup-dev-auth.sh status
#
# Run once. The association survives frontend deploys — the GitHub Actions
# workflow only syncs S3 and invalidates the cache, it does not touch the
# distribution config, so the gate stays up until explicitly removed.
#
# CloudFront Functions are a global (us-east-1) resource, like the distribution.

set -euo pipefail

ACTION="${1:-status}"
FUNCTION_NAME="memories-dev-basic-auth"
DISTRIBUTION_ID="${DISTRIBUTION_ID:-E36EDVF1YGMEII}"   # memories.wrightideas.biz
REGION="us-east-1"
SRC="$(dirname "$0")/cloudfront-basic-auth.js"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── Read the live distribution config ────────────────────────────────────────
# Every update needs the current ETag, and CloudFront rejects the call if the
# config has changed since it was read.
fetch_config() {
  aws cloudfront get-distribution-config \
    --id "$DISTRIBUTION_ID" --region "$REGION" \
    --output json > "$TMP/full.json"
  python3 - "$TMP/full.json" "$TMP/etag" "$TMP/config.json" <<'PY'
import json, sys
full = json.load(open(sys.argv[1]))
open(sys.argv[2], "w").write(full["ETag"])
json.dump(full["DistributionConfig"], open(sys.argv[3], "w"))
PY
}

# ── Rewrite the viewer-request association ───────────────────────────────────
# Only DefaultCacheBehavior is touched. This distribution has no additional
# cache behaviours; if that changes, they need the same association or they
# become an unprotected path to the origin.
set_association() {
  local arn="$1"   # empty string removes the association
  python3 - "$TMP/config.json" "$arn" <<'PY'
import json, sys
path, arn = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
beh = cfg["DefaultCacheBehavior"]
beh["FunctionAssociations"] = (
    {"Quantity": 1, "Items": [{"FunctionARN": arn, "EventType": "viewer-request"}]}
    if arn else {"Quantity": 0}
)
json.dump(cfg, open(path, "w"))
PY
  aws cloudfront update-distribution \
    --id "$DISTRIBUTION_ID" --region "$REGION" \
    --distribution-config "file://$TMP/config.json" \
    --if-match "$(cat "$TMP/etag")" \
    --output text --query 'Distribution.Status'
}

case "$ACTION" in

  enable)
    : "${DEV_AUTH_USER:?Set DEV_AUTH_USER}"
    : "${DEV_AUTH_PASS:?Set DEV_AUTH_PASS}"

    # The function has no btoa and a tight CPU budget, so the credential is
    # encoded here and embedded as a literal.
    B64=$(printf '%s:%s' "$DEV_AUTH_USER" "$DEV_AUTH_PASS" | base64)
    sed "s|__BASIC_AUTH_B64__|${B64}|" "$SRC" > "$TMP/fn.js"

    if aws cloudfront describe-function --name "$FUNCTION_NAME" --region "$REGION" >/dev/null 2>&1; then
      ETAG=$(aws cloudfront describe-function --name "$FUNCTION_NAME" --region "$REGION" \
               --query 'ETag' --output text)
      echo "↻ Updating function $FUNCTION_NAME"
      aws cloudfront update-function \
        --name "$FUNCTION_NAME" --region "$REGION" \
        --function-config Comment="Basic auth for dev preview",Runtime=cloudfront-js-2.0 \
        --function-code "fileb://$TMP/fn.js" \
        --if-match "$ETAG" --output text --query 'FunctionSummary.Name' >/dev/null
    else
      echo "+ Creating function $FUNCTION_NAME"
      aws cloudfront create-function \
        --name "$FUNCTION_NAME" --region "$REGION" \
        --function-config Comment="Basic auth for dev preview",Runtime=cloudfront-js-2.0 \
        --function-code "fileb://$TMP/fn.js" \
        --output text --query 'FunctionSummary.Name' >/dev/null
    fi

    # A function must be published to LIVE before it can be associated.
    ETAG=$(aws cloudfront describe-function --name "$FUNCTION_NAME" --region "$REGION" \
             --query 'ETag' --output text)
    ARN=$(aws cloudfront publish-function \
            --name "$FUNCTION_NAME" --region "$REGION" --if-match "$ETAG" \
            --query 'FunctionSummary.FunctionMetadata.FunctionARN' --output text)
    echo "✓ Published $ARN"

    fetch_config
    echo "→ Associating with distribution $DISTRIBUTION_ID"
    set_association "$ARN"
    echo ""
    echo "✓ Basic auth enabled. Username: $DEV_AUTH_USER"
    echo "  CloudFront takes a few minutes to propagate to all edge locations."
    ;;

  disable)
    fetch_config
    echo "→ Removing viewer-request association from $DISTRIBUTION_ID"
    set_association ""
    echo "✓ Basic auth removed — the site is publicly reachable again."
    ;;

  status)
    fetch_config
    python3 - "$TMP/config.json" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
fa = cfg["DefaultCacheBehavior"].get("FunctionAssociations", {})
if fa.get("Quantity"):
    for item in fa["Items"]:
        print(f"ENABLED  {item['EventType']}  {item['FunctionARN']}")
else:
    print("DISABLED — the site is publicly reachable")
PY
    ;;

  *)
    echo "Usage: $0 {enable|disable|status}"
    exit 1
    ;;
esac
