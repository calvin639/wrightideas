.PHONY: build deploy deploy-guided deploy-prod validate local-api setup-secrets clean logs help request-cert cert-dns wait-cert

# ── CONFIG ────────────────────────────────────────────────────────────────────
STACK_DEV    = memories-in-stone-dev
STACK_PROD   = memories-in-stone-prod
REGION       = eu-west-1
ENV         ?= dev

# ── HELP ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  Memories in Stone — Backend Commands"
	@echo "  ─────────────────────────────────────────"
	@echo "  make build           Build all Lambda functions"
	@echo "  make deploy          Deploy to dev (after first guided deploy)"
	@echo "  make deploy-guided   Interactive first-time deploy (dev)"
	@echo "  make deploy-prod     Deploy to production"
	@echo "  make validate        Lint and validate the SAM template"
	@echo "  make local-api       Run API locally on port 3001"
	@echo "  make setup-secrets   Store all required SSM secrets (interactive)"
	@echo "  make request-cert    Request ACM cert for memories.wrightideas.biz (run once)"
	@echo "  make cert-dns        Print the Cloudflare DNS validation record"
	@echo "  make wait-cert       Wait until the ACM cert is validated and ready"
	@echo "  make logs fn=<name>  Tail CloudWatch logs for a function"
	@echo "  make clean           Remove build artifacts"
	@echo ""

# ── BUILD ─────────────────────────────────────────────────────────────────────
build:
	sam build --cached --parallel

# ── VALIDATE ──────────────────────────────────────────────────────────────────
validate:
	sam validate --lint

# ── DEPLOY ────────────────────────────────────────────────────────────────────
deploy: build
	@chmod +x scripts/deploy.sh && bash -i scripts/deploy.sh dev

deploy-guided: build
	@chmod +x scripts/deploy.sh && bash -i scripts/deploy.sh dev

deploy-prod: build
	@echo "⚠️  Deploying to PRODUCTION. Press Ctrl+C to cancel."
	@sleep 3
	@chmod +x scripts/deploy.sh && bash -i scripts/deploy.sh prod

# ── LOCAL DEV ─────────────────────────────────────────────────────────────────
local-api: build
	sam local start-api --port 3001 --env-vars env.local.json

local-invoke:
	@test -n "$(fn)" || (echo "Usage: make local-invoke fn=FunctionName event=events/file.json" && exit 1)
	sam local invoke $(fn) --event $(event) --env-vars env.local.json

# ── ACM CERTIFICATE (memories.wrightideas.biz — run once before first prod deploy) ──

# Request the cert and print the ARN to add to ~/.bashrc as ACM_CERT_ARN
request-cert:
	@echo "Checking for existing ACM certificate for memories.wrightideas.biz..."
	@EXISTING=$$(aws acm list-certificates --region us-east-1 \
	  --query "CertificateSummaryList[?DomainName=='memories.wrightideas.biz'].CertificateArn" \
	  --output text); \
	if [ -n "$$EXISTING" ]; then \
	  echo "✅ Certificate already exists: $$EXISTING"; \
	  echo ""; \
	  echo "Add to ~/.bashrc if not already there:"; \
	  echo "  export ACM_CERT_ARN=$$EXISTING"; \
	else \
	  ARN=$$(aws acm request-certificate \
	    --domain-name memories.wrightideas.biz \
	    --validation-method DNS \
	    --region us-east-1 \
	    --query CertificateArn --output text); \
	  echo "✅ Certificate requested: $$ARN"; \
	  echo ""; \
	  echo "Add to ~/.bashrc:"; \
	  echo "  export ACM_CERT_ARN=$$ARN"; \
	  echo ""; \
	  echo "Then run:  make cert-dns   to get the Cloudflare validation record"; \
	fi

# Print the CNAME record to add to Cloudflare for DNS validation
cert-dns:
	@test -n "$(ACM_CERT_ARN)" || (echo "❌ ACM_CERT_ARN not set. Run 'make request-cert' first." && exit 1)
	@echo "Add this CNAME to Cloudflare (DNS only — grey cloud):"
	@echo ""
	@aws acm describe-certificate \
	  --certificate-arn $(ACM_CERT_ARN) \
	  --region us-east-1 \
	  --query 'Certificate.DomainValidationOptions[0].ResourceRecord' \
	  --output table
	@echo ""
	@echo "Once added, run:  make wait-cert"

# Block until ACM marks the cert as ISSUED
wait-cert:
	@test -n "$(ACM_CERT_ARN)" || (echo "❌ ACM_CERT_ARN not set. Run 'make request-cert' first." && exit 1)
	@echo "Waiting for certificate to be validated (may take 1-2 min after adding the CNAME)..."
	@aws acm wait certificate-validated \
	  --certificate-arn $(ACM_CERT_ARN) \
	  --region us-east-1
	@echo "✅ Certificate validated — ready to deploy-prod"

# ── SECRETS SETUP ─────────────────────────────────────────────────────────────
# Run this once to store secrets in AWS SSM Parameter Store
setup-secrets:
	@echo "Setting up SSM parameters for environment: $(ENV)"
	@read -p "Stripe Secret Key (sk_...): " stripe_key && \
		aws ssm put-parameter --name "/memories/$(ENV)/stripe-secret-key" \
		--value "$$stripe_key" --type SecureString --overwrite --region $(REGION)
	@read -p "Stripe Webhook Secret (whsec_...): " stripe_wh && \
		aws ssm put-parameter --name "/memories/$(ENV)/stripe-webhook-secret" \
		--value "$$stripe_wh" --type SecureString --overwrite --region $(REGION)
	@read -p "Runway API Key: " runway_key && \
		aws ssm put-parameter --name "/memories/$(ENV)/runway-api-key" \
		--value "$$runway_key" --type SecureString --overwrite --region $(REGION)
	@read -p "API Domain (e.g. api.memories.wrightideas.co or your API GW URL): " api_domain && \
		aws ssm put-parameter --name "/memories/$(ENV)/api-domain" \
		--value "$$api_domain" --type String --overwrite --region $(REGION)
	@echo "✅ All secrets stored in SSM"

# ── LOGS ──────────────────────────────────────────────────────────────────────
logs:
	@test -n "$(fn)" || (echo "Usage: make logs fn=FunctionName" && exit 1)
	sam logs -n $(fn) --stack-name $(STACK_DEV) --tail --region $(REGION)

logs-prod:
	@test -n "$(fn)" || (echo "Usage: make logs-prod fn=FunctionName" && exit 1)
	sam logs -n $(fn) --stack-name $(STACK_PROD) --tail --region $(REGION)

# ── CLEAN ─────────────────────────────────────────────────────────────────────
clean:
	rm -rf .aws-sam
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# ── STACK STATUS ──────────────────────────────────────────────────────────────
status:
	aws cloudformation describe-stacks --stack-name $(STACK_DEV) \
		--region $(REGION) --query 'Stacks[0].StackStatus' --output text

outputs:
	aws cloudformation describe-stacks --stack-name $(STACK_DEV) \
		--region $(REGION) --query 'Stacks[0].Outputs' --output table
