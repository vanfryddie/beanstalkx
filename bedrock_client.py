"""Amazon Bedrock — the one call the Cross Learning Model makes to a
language model, and everything that keeps that call from hurting the page.

The model is asked to weigh and explain figures the app has already
computed. It never computes anything the page relies on, so this module
is deliberately small: one function that sends a JSON payload and gets a
JSON object back, or gets None and a reason. Every failure — not
configured, no IAM permission, model access not enabled, a timeout, a
refusal, a reply that is not the JSON that was asked for — comes back as
None, because the tab must look exactly as it did before this existed
when the model cannot be reached. Nothing here raises to the caller.

Why InvokeModel on bedrock-runtime and not Converse: Anthropic's Bedrock
documentation lists the current Claude models (Opus 5.5, Sonnet 5.5,
Haiku 5.5) as reachable through InvokeModel on bedrock-runtime, and does
not say the same of Converse. InvokeModel is in the boto3 already pinned
in requirements.txt, so this needs no new dependency.

Configuration is environment only, set on the Elastic Beanstalk
environment rather than written here, because Bedrock model ids are
region-prefixed and change, and the right one is whatever the account
actually offers:

    LDIS_BEDROCK_MODEL_ID   the inference profile id (or ARN) to call.
                            Unset means the feature is off.
    AWS_REGION              the region, shared with the S3 client.
    LDIS_BEDROCK_TIMEOUT    seconds to wait for a reply (default 60; keep it
                            well under gunicorn's --timeout 120 in Procfile).
    LDIS_BEDROCK_ANTHROPIC_VERSION
                            the request body's anthropic_version
                            (default bedrock-2023-05-31).
"""

import hashlib
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DEFAULT_ANTHROPIC_VERSION = "bedrock-2023-05-31"

# An answer is reused for this long when the payload is byte-identical.
# The payload is built from the data on the page, so an upload that
# changes the data changes the key and the next view asks afresh; a
# manager reloading the tab ten times does not pay for ten calls.
CACHE_SECONDS = 12 * 60 * 60
CACHE_MAX = 200

_client = None
_client_error = None
_client_lock = threading.Lock()
_cache = {}
_cache_lock = threading.Lock()

# Tests replace this with a function(body) -> reply dict, shaped like the
# decoded InvokeModel response body, or one that raises. Production
# leaves it None and the real client is used.
_transport = None


def model_id():
    return (os.environ.get("LDIS_BEDROCK_MODEL_ID") or "").strip() or None


def enabled():
    """Whether the page should offer generated explanations at all. A
    stub counts, so a test can exercise the whole path without AWS."""
    return _transport is not None or model_id() is not None


def set_transport(fn):
    """Install a stub for tests (None restores the real client), and
    forget cached answers so one test's reply never leaks into the next."""
    global _transport
    _transport = fn
    clear_cache()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _get_client():
    """The bedrock-runtime client, built once. Retries are off: a slow
    model retried twice is a page section that takes three times as long
    to give up, and the section is optional."""
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client
    with _client_lock:
        if _client is not None or _client_error is not None:
            return _client
        try:
            import boto3
            from botocore.config import Config
            timeout = _timeout()
            # Pool size covers the review's batches running side by side
            # (CLM_LLM_BATCH in application.py) plus a forecast.
            _client = boto3.client(
                "bedrock-runtime",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
                config=Config(connect_timeout=5, read_timeout=timeout, max_pool_connections=10,
                              retries={"max_attempts": 1, "mode": "standard"}),
            )
        except Exception as exc:  # packaging or credential-chain problems
            _client_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Bedrock client unavailable: %s", _client_error)
    return _client


def _timeout():
    try:
        return max(5.0, float(os.environ.get("LDIS_BEDROCK_TIMEOUT") or DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


def _send(body):
    """One InvokeModel call. Returns the decoded reply body."""
    if _transport is not None:
        return _transport(body)
    client = _get_client()
    if client is None:
        raise RuntimeError(_client_error or "Bedrock client unavailable")
    response = client.invoke_model(
        modelId=model_id(), contentType="application/json", accept="application/json",
        body=json.dumps(body))
    return json.loads(response["body"].read())


def _classify(exc):
    """A short, stable reason for the log and for the page's quiet
    "not available" note. The three set-up failures read alike in the raw
    error, and telling them apart is most of what fixing them takes."""
    # botocore's timeout errors carry no response at all, and a classifier
    # that raised here would turn every slow model into an exception —
    # the one thing this module promises never to hand the caller.
    response = getattr(exc, "response", None)
    code = ((response or {}).get("Error") or {}).get("Code") if isinstance(response, dict) else None
    message = str(exc)
    if code == "AccessDeniedException" and "aws-marketplace" in message:
        return "marketplace"    # first use of the model needs Marketplace permissions
    if code == "AccessDeniedException" and "not authorized to perform" in message:
        return "iam"            # the instance profile lacks bedrock:InvokeModel
    if code == "AccessDeniedException":
        return "model_access"   # model access not granted for this account/region
    if code == "ValidationException" and "on-demand" in message:
        return "needs_profile"  # a bare model id where an inference profile is needed
    if code in ("ThrottlingException", "ServiceQuotaExceededException"):
        return "throttled"
    if "timed out" in message.lower() or type(exc).__name__ in ("ReadTimeoutError", "ConnectTimeoutError"):
        return "timeout"
    return "error"


def extract_json(text):
    """The first JSON object in the model's reply. The prompt asks for
    bare JSON; this tolerates a code fence or a sentence around it,
    because a reply that is right but wrapped is still right."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth, in_string, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                        return value if isinstance(value, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def ask_json(system, payload, max_tokens=8000):
    """Send `payload` (a JSON-able dict) under `system`; return
    (answer_dict, status). status is "ok", "cached", "off", or the reason
    it failed. answer_dict is None unless status is ok or cached."""
    if not enabled():
        return None, "off"
    user_text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    key = hashlib.sha256("\x00".join((model_id() or "stub", system, user_text)).encode()).hexdigest()
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1], "cached"

    body = {
        "anthropic_version": os.environ.get("LDIS_BEDROCK_ANTHROPIC_VERSION") or DEFAULT_ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    started = time.time()
    try:
        reply = _send(body)
    except Exception as exc:
        try:
            reason = _classify(exc)
        except Exception:
            reason = "error"
        logger.warning("Bedrock call failed (%s) after %.1fs: %s", reason, time.time() - started, exc)
        return None, reason

    stop = reply.get("stop_reason")
    if stop == "refusal":
        logger.warning("Bedrock declined the request: %s", reply.get("stop_details"))
        return None, "refused"
    text = "".join(block.get("text", "") for block in reply.get("content") or []
                   if isinstance(block, dict) and block.get("type") == "text")
    answer = extract_json(text)
    if answer is None:
        logger.warning("Bedrock reply was not JSON (stop_reason=%s, %d chars)", stop, len(text))
        return None, "max_tokens" if stop == "max_tokens" else "bad_json"
    logger.info("Bedrock answered in %.1fs (usage %s)", time.time() - started, reply.get("usage"))
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, answer)
    return answer, "ok"
