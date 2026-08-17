"""GitHub push webhook: pull the vault repo as soon as it changes upstream.

When the vault is a git clone kept in sync from a remote (see DEPLOYMENT.md), the
server's view of it goes stale between polls. This endpoint lets GitHub tell the
server "the vault moved" so it can `git pull --ff-only` immediately.

The route is deliberately UNAUTHENTICATED as far as the bearer middleware is
concerned -- GitHub cannot present a bearer token -- so the HMAC signature is the
*only* thing standing between the public internet and a subprocess call. Every
design choice below follows from that:

  * the shared secret is required; with WEBHOOK_SECRET unset the endpoint fails
    CLOSED (503) and never touches git;
  * the signature is checked over the RAW body, before the JSON is parsed, so a
    forged payload is rejected before any parser sees it;
  * the body is size-capped before it is read into memory;
  * the git command is a fixed argv (no shell) whose only variable is the
    operator-configured vault path -- nothing from the payload reaches it.

Everything the payload controls is therefore limited to "should we pull or not".
"""

import hashlib
import hmac
import json
import logging
import subprocess
import threading

from starlette.responses import JSONResponse

from . import config

logger = logging.getLogger(__name__)

# Path the webhook is served on. Kept here (not inlined) because auth.py must exempt
# exactly this path and server.build_app() must mount exactly this path -- the two
# have to agree or the endpoint is either unreachable or an auth hole.
WEBHOOK_PATH = "/webhooks/github"

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"

# GitHub caps deliveries at 25 MB but a push payload is orders of magnitude smaller.
# Cap what we buffer so an unauthenticated caller cannot make the server hold a large
# body in memory just to fail the signature check.
MAX_BODY_BYTES = 2_000_000

# One pull at a time. A burst of pushes (or a GitHub redelivery) would otherwise run
# concurrent `git pull`s on the same worktree, which fight over index.lock. Overlapping
# deliveries return "skipped" immediately rather than queueing -- the in-flight pull is
# already fetching everything they would have fetched.
_pull_lock = threading.Lock()


def _ok(status: str, **extra) -> JSONResponse:
    """200 with a JSON status body.

    Handled-but-not-pulled outcomes are 200 on purpose: GitHub retries nothing on 2xx,
    and for "wrong branch", "not a push", or "pull failed" a retry would fail exactly
    the same way. Only a genuinely unauthenticated or unconfigured request gets 4xx/5xx.
    """
    return JSONResponse({"status": status, **extra})


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    """Constant-time check of GitHub's X-Hub-Signature-256 over the raw body.

    Returns False for a missing/malformed header rather than raising, so the caller
    treats "no signature" and "wrong signature" identically -- an unauthenticated
    caller learns nothing from the difference.
    """
    if not secret or not header:
        return False
    scheme, _, sent = header.partition("=")
    if scheme != "sha256" or not sent:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)


def _git(*args: str, timeout: float) -> subprocess.CompletedProcess:
    """Run git against the vault with no shell and a hard timeout."""
    return subprocess.run(
        ["git", "-C", str(config.VAULT_PATH), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _head() -> str | None:
    """Current vault HEAD sha, or None when the vault is not a usable git repo."""
    try:
        result = _git("rev-parse", "HEAD", timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[webhook] could not read HEAD: %s", type(e).__name__)
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def pull_vault() -> dict:
    """Fast-forward the vault to its remote. Never raises; always returns a status dict.

    Idempotent: pulling an already-current repo is a no-op that reports 0 commits.
    Uses --ff-only so a diverged worktree fails loudly instead of auto-merging and
    silently rewriting vault files the operator has not seen.
    """
    if not _pull_lock.acquire(blocking=False):
        logger.info("[webhook] pull already in progress, skipping this delivery")
        return {"status": "skipped", "reason": "pull already in progress"}

    try:
        before = _head()
        try:
            result = _git("pull", "--ff-only", timeout=config.webhook_timeout())
        except subprocess.TimeoutExpired:
            logger.error("[webhook] git pull timed out after %ss", config.webhook_timeout())
            return {"status": "failed", "reason": "git pull timed out"}
        except OSError as e:
            logger.error("[webhook] git pull could not run: %s", type(e).__name__)
            return {"status": "failed", "reason": "git unavailable"}

        if result.returncode != 0:
            # Most likely a diverged worktree (--ff-only refuses) or a credential
            # problem. Log stderr for debugging; return a reason without it, since the
            # caller is unauthenticated and git errors can echo the remote URL (which
            # carries the token in this deployment).
            logger.error(
                "[webhook] git pull failed (exit %s): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return {"status": "failed", "reason": "git pull failed"}

        after = _head()
        commits = 0
        if before and after and before != after:
            try:
                count = _git("rev-list", "--count", f"{before}..{after}", timeout=10)
                if count.returncode == 0:
                    commits = int(count.stdout.strip() or 0)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                # The pull itself succeeded; a bad count must not turn that into a failure.
                logger.warning("[webhook] pulled but could not count commits")

        logger.info("[webhook] pulled %s commit(s), HEAD now %s", commits, (after or "?")[:8])
        return {"status": "pulled", "commits": commits}
    finally:
        _pull_lock.release()


def _decide(event: str | None, body: bytes) -> str | None:
    """Return a skip reason for deliveries we do not act on, or None to pull."""
    if event != "push":
        return f"event {event or 'unknown'!s} is not push"

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "payload is not valid JSON"
    if not isinstance(payload, dict):
        return "payload is not a JSON object"

    ref = payload.get("ref")
    expected = f"refs/heads/{config.webhook_branch()}"
    if ref != expected:
        return f"ref {ref!r} is not {expected!r}"
    if payload.get("deleted"):
        return "branch was deleted"
    return None


async def github_webhook(request):
    """POST /webhooks/github -- pull the vault on a push to the watched branch.

    Routed for every HTTP method (see server.build_app) so that no other method can fall
    through this auth-exempt path to the MCP transport; non-POST is rejected here.
    """
    if request.method != "POST":
        return JSONResponse(
            {"status": "error", "reason": "method not allowed"},
            status_code=405,
            headers={"Allow": "POST"},
        )

    secret = config.WEBHOOK_SECRET
    if not secret:
        # Fail CLOSED. Without a secret every caller's signature would be unverifiable,
        # so the endpoint must refuse rather than pull for anyone who asks.
        logger.error("[webhook] WEBHOOK_SECRET is not set; refusing the delivery")
        return JSONResponse(
            {"status": "error", "reason": "webhook not configured"}, status_code=503
        )

    # Reject an oversized body on the declared length before reading it, then re-check
    # the real length in case Content-Length lied or was absent (chunked encoding).
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        logger.warning("[webhook] rejected oversized delivery (%s bytes declared)", declared)
        return JSONResponse({"status": "error", "reason": "payload too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        logger.warning("[webhook] rejected oversized delivery (%s bytes)", len(body))
        return JSONResponse({"status": "error", "reason": "payload too large"}, status_code=413)

    if not verify_signature(body, request.headers.get(SIGNATURE_HEADER), secret):
        logger.warning("[webhook] rejected delivery with a missing or invalid signature")
        return JSONResponse({"status": "error", "reason": "invalid signature"}, status_code=401)

    event = request.headers.get(EVENT_HEADER)
    skip = _decide(event, body)
    if skip:
        logger.info("[webhook] skipped: %s", skip)
        return _ok("skipped", reason=skip)

    logger.info("[webhook] push to %s, pulling vault", config.webhook_branch())
    # The pull is blocking subprocess work; run it off the event loop so a slow git
    # fetch cannot stall every concurrent MCP request.
    from starlette.concurrency import run_in_threadpool

    # pull_vault already reports its own {"status": ...} and never raises, so its result
    # is the response body as-is (200 for pulled/skipped/failed alike -- see _ok).
    return JSONResponse(await run_in_threadpool(pull_vault))
