"""Tickety integration — creates a real Tickety ticket whenever an
escalation ticket is logged in the app, so the two stay linked.

Uses SigV4A (not standard SigV4) against the global endpoint, per
Tickety's documented setup: SigV4A signs requests region-agnostically,
so they keep working through Tickety's regional-redirection failover
during an outage, whereas a standard SigV4-signed request tied to one
region would 403 the moment traffic redirects elsewhere.

VENDORED SERVICE MODEL: botocore has no built-in knowledge of the
"tickety" service (it's an Amazon-internal API, not a public AWS
service) — calling create_client(service_name="tickety") only works
once botocore can find a service-2.json describing its operations.
TicketyPythonSdk normally provides this, but it isn't on public PyPI,
so this module points botocore at vendor/tickety_service_model/ (via
the AWS_DATA_PATH mechanism, the same one botocore itself uses for any
customer-vendored service model) — see vendor/tickety_service_model/README.md
for exactly what file needs to go there and why it isn't included here.
"""

import logging
import os

logger = logging.getLogger(__name__)

TICKETY_ENDPOINT = "https://global.api.tickety.amazon.dev"
TICKETY_REGION = "global"

_VENDOR_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "tickety_service_model")

_client = None
_client_error = None


def get_tickety_client():
    """Lazily creates and caches the boto3 Tickety client. Returns None
    (and logs why) if the vendored service model, the CRT dependency,
    or the SDK isn't available, rather than raising — a Tickety outage
    or a packaging problem should never take down escalation creation
    in this app, only skip the external sync."""
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client
    try:
        import botocore.session
        from botocore.config import Config

        # Extend (not replace) whatever AWS_DATA_PATH is already set on
        # the instance, so this doesn't clobber some other vendored
        # service model that might also be in use.
        existing = os.environ.get("AWS_DATA_PATH", "")
        os.environ["AWS_DATA_PATH"] = (
            _VENDOR_MODEL_PATH + os.pathsep + existing if existing else _VENDOR_MODEL_PATH
        )

        session = botocore.session.get_session()
        config = Config(signature_version="v4a", retries={"max_attempts": 3})
        _client = session.create_client(
            region_name=TICKETY_REGION,
            service_name="tickety",
            endpoint_url=TICKETY_ENDPOINT,
            config=config,
        )
        return _client
    except Exception as e:
        _client_error = str(e)
        logger.warning("Tickety client unavailable: %s", e)
        return None


def create_tickety_ticket(title, description, category, type_, item, severity="SEV_4"):
    """Creates a real Tickety ticket. Returns (ticket_id, None) on
    success or (None, error_message) on failure — callers decide how
    loudly to surface that, but it should never raise into the caller,
    since a Tickety-side failure must not block creating the app's own
    internal escalation record."""
    client = get_tickety_client()
    if client is None:
        return None, _client_error or "Tickety client not configured"
    try:
        response = client.create_ticket(
            awsAccountId="Default",
            ticketingSystemName="Default",
            title=title[:200],
            description=description or "",
            categorization={"category": category, "type": type_, "item": item},
            severity=severity,
        )
        return response.get("id"), None
    except Exception as e:
        logger.warning("Tickety create_ticket failed: %s", e)
        return None, str(e)


def tickety_link(ticket_id):
    if not ticket_id:
        return None
    return f"https://tickety.amazon.dev/tickets/{ticket_id}"
