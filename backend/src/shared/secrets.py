"""
Secret accessor — reads values injected as Lambda environment variables at deploy time.

Secrets are passed as CloudFormation NoEcho parameters and stored as encrypted
Lambda environment variables (KMS-encrypted at rest by AWS automatically).

No Secrets Manager or SSM calls needed at runtime.
"""
import os


def get_stripe_key() -> str:
    val = os.environ.get("STRIPE_SECRET_KEY", "")
    if not val or val.startswith("placeholder"):
        raise EnvironmentError("STRIPE_SECRET_KEY not configured")
    return val


def get_fal_key() -> str:
    """fal.ai API key — the video generation provider.

    Accepts FAL_KEY (fal's own convention, what the SDK and probe scripts read)
    or FAL_AI_API_KEY, which is what the key is called in the shell profile.
    Supporting both stops a working key in the wrong variable name from looking
    like a missing key.
    """
    val = os.environ.get("FAL_KEY", "") or os.environ.get("FAL_AI_API_KEY", "")
    if not val or val.startswith("placeholder"):
        raise EnvironmentError("FAL_KEY not configured")
    return val


def get_runway_key() -> str:
    """Deprecated — Runway's route to seedance blocks photos of real people.
    Kept so anything still importing it fails loudly rather than silently."""
    val = os.environ.get("RUNWAY_API_KEY", "")
    if not val or val.startswith("placeholder"):
        raise EnvironmentError("RUNWAY_API_KEY not configured")
    return val


def get_stripe_webhook_secret() -> str:
    val = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not val or val.startswith("whsec_placeholder"):
        raise EnvironmentError("STRIPE_WEBHOOK_SECRET not configured — update after first deploy")
    return val


def get_runway_webhook_url() -> str:
    url = os.environ.get("RUNWAY_WEBHOOK_URL", "")
    if not url or "placeholder" in url:
        raise EnvironmentError("RUNWAY_WEBHOOK_URL not configured — update after first deploy")
    return url
