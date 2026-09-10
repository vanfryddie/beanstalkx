"""
Decodes and verifies the ID token ALB's built-in OIDC action injects into
every authenticated request as the ``x-amzn-oidc-data`` header, once the
HTTPS:443 listener has "Authenticate user" configured against Federate.

ALB signs this JWT itself (re-signing the IdP's token with its own
per-region key), so verifying it only requires ALB's public key — no
call back to Federate needed on every request. Docs:
https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html
"""
import os
import json
import base64
import urllib.request

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key

REGION = os.environ.get("AWS_REGION", "eu-west-1")
ALB_PUBLIC_KEY_ENDPOINT = f"https://public-keys.auth.elb.{REGION}.amazonaws.com/"

_key_cache = {}


def _get_alb_public_key(kid):
    """ALB's public-key endpoint returns a PEM-encoded EC public key
    directly (not a JWKS), keyed by the kid in the token header."""
    if kid in _key_cache:
        return _key_cache[kid]
    with urllib.request.urlopen(ALB_PUBLIC_KEY_ENDPOINT + kid, timeout=5) as resp:
        pem = resp.read().decode("utf-8")
    key = load_pem_public_key(pem.encode("utf-8"))
    _key_cache[kid] = key
    return key


def decode_oidc_header(header_value):
    """Verify and decode the x-amzn-oidc-data header. Returns the claims
    dict, or None if the header is missing/invalid (e.g. accessed directly
    without going through the authenticated ALB listener)."""
    if not header_value:
        return None
    try:
        headers = jwt.get_unverified_header(header_value)
        kid = headers["kid"]
        key = _get_alb_public_key(kid)
        claims = jwt.decode(header_value, key=key, algorithms=["ES256"])
        return claims
    except Exception:
        return None
