"""GitHub push webhook: signature gate, event filtering, and the real git pull.

Two layers here, on purpose:

  * handler tests drive the fully assembled app from build_app() -- transport + OAuth +
    bearer middleware -- so they fail if the webhook ever stops being reachable without
    a bearer token, or if the rest of the surface stops requiring one;
  * pull tests run against real git repositories (no subprocess mocking), so the
    fast-forward, already-up-to-date, and diverged cases are exercised as they will
    behave on the server rather than as we imagine they behave.
"""

import hashlib
import hmac
import importlib
import json
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from obsidian_vault_mcp import auth, config, server, webhook

SECRET = "test-webhook-secret"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def sign(body: bytes, secret: str = SECRET) -> str:
    """Build the X-Hub-Signature-256 value GitHub would send for this body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def push_payload(ref: str = "refs/heads/main", **extra) -> bytes:
    """A minimal push payload -- only the fields the handler actually inspects."""
    return json.dumps({"ref": ref, "repository": {"full_name": "owner/vault"}, **extra}).encode()


def post(client, body: bytes, *, event="push", signature=..., headers=None):
    """POST a delivery, signed correctly unless the caller overrides `signature`."""
    hdrs = {"Content-Type": "application/json"}
    if event is not None:  # event=None models a delivery with no event header at all
        hdrs["X-GitHub-Event"] = event
    if signature is ...:
        signature = sign(body)
    if signature is not None:
        hdrs["X-Hub-Signature-256"] = signature
    hdrs.update(headers or {})
    return client.post(webhook.WEBHOOK_PATH, content=body, headers=hdrs)


def fresh_app():
    """Build the assembled app on a brand-new FastMCP instance.

    server.mcp is a module-level singleton whose StreamableHTTPSessionManager refuses to
    run twice, so every test that starts a TestClient (which runs the lifespan) needs the
    module reloaded first -- the same reason test_mcp_path.py reloads.
    """
    importlib.reload(server)
    return server.build_app()


@pytest.fixture
def app(monkeypatch):
    """The assembled app with a configured webhook and a stubbed pull.

    The pull itself is covered by the real-git tests below; stubbing it here keeps the
    handler tests focused on the signature/filter decisions and off the filesystem.
    """
    monkeypatch.setattr(config, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "WEBHOOK_BRANCH", "main")
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "bearer-token-for-tests")
    calls = []

    def fake_pull():
        calls.append(1)
        return {"status": "pulled", "commits": 3}

    monkeypatch.setattr(webhook, "pull_vault", fake_pull)
    built = fresh_app()
    built.state.pull_calls = calls
    return built


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# --- signature verification -------------------------------------------------

def test_valid_signature_pulls(client):
    r = post(client, push_payload())
    assert r.status_code == 200
    assert r.json() == {"status": "pulled", "commits": 3}
    assert len(client.app.state.pull_calls) == 1


def test_invalid_signature_is_401_and_does_not_pull(client):
    body = push_payload()
    r = post(client, body, signature=sign(body, "wrong-secret"))
    assert r.status_code == 401
    assert r.json()["reason"] == "invalid signature"
    assert client.app.state.pull_calls == []


def test_missing_signature_is_401(client):
    r = post(client, push_payload(), signature=None)
    assert r.status_code == 401
    assert client.app.state.pull_calls == []


def test_signature_for_a_different_body_is_rejected(client):
    """The MAC must cover the body actually sent, not just be well-formed."""
    r = post(client, push_payload(), signature=sign(push_payload("refs/heads/other")))
    assert r.status_code == 401
    assert client.app.state.pull_calls == []


@pytest.mark.parametrize("bad", ["", "sha256=", "deadbeef", "sha1=deadbeef", "sha256"])
def test_malformed_signature_headers_are_rejected(client, bad):
    r = post(client, push_payload(), signature=bad)
    assert r.status_code == 401
    assert client.app.state.pull_calls == []


def test_verify_signature_unit():
    body = b'{"ref":"refs/heads/main"}'
    assert webhook.verify_signature(body, sign(body), SECRET)
    assert not webhook.verify_signature(body, sign(body, "other"), SECRET)
    assert not webhook.verify_signature(body, None, SECRET)
    # No configured secret must never validate, even against a "correct" MAC of "".
    assert not webhook.verify_signature(body, sign(body, ""), "")


# --- event and branch filtering ---------------------------------------------

@pytest.mark.parametrize("event", ["ping", "pull_request", "issues", "star", None])
def test_non_push_events_are_acknowledged_without_pulling(client, event):
    r = post(client, push_payload(), event=event)
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
    assert client.app.state.pull_calls == []


@pytest.mark.parametrize(
    "ref",
    ["refs/heads/develop", "refs/heads/feature/x", "refs/tags/v1.0", "refs/heads/mainline"],
)
def test_pushes_to_other_refs_are_skipped(client, ref):
    r = post(client, push_payload(ref))
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
    assert client.app.state.pull_calls == []


def test_push_to_main_pulls(client):
    assert post(client, push_payload("refs/heads/main")).json()["status"] == "pulled"
    assert len(client.app.state.pull_calls) == 1


def test_branch_deletion_is_skipped(client):
    """A delete push carries the watched ref but nothing to fast-forward to."""
    r = post(client, push_payload("refs/heads/main", deleted=True))
    assert r.json()["status"] == "skipped"
    assert client.app.state.pull_calls == []


def test_watched_branch_is_configurable(client, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_BRANCH", "production")
    assert post(client, push_payload("refs/heads/main")).json()["status"] == "skipped"
    assert post(client, push_payload("refs/heads/production")).json()["status"] == "pulled"


@pytest.mark.parametrize("body", [b"not json", b"[]", b"", b'{"ref":'])
def test_unparseable_payloads_are_skipped_not_crashed(client, body):
    r = post(client, body)
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
    assert client.app.state.pull_calls == []


# --- fail-closed and hardening ----------------------------------------------

def test_unconfigured_secret_refuses_and_never_pulls(client, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "")
    body = push_payload()
    # Even an unsigned request must not be treated as "no signature required".
    assert post(client, body, signature=None).status_code == 503
    assert post(client, body).status_code == 503
    assert client.app.state.pull_calls == []


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_non_post_methods_are_405_and_do_not_reach_the_transport(client, method):
    """The auth-exempt path must not fall through to the MCP transport on any method."""
    r = getattr(client, method)(webhook.WEBHOOK_PATH)
    assert r.status_code == 405
    assert r.json()["reason"] == "method not allowed"


def test_oversized_payload_is_rejected_before_pulling(client):
    body = b"x" * (webhook.MAX_BODY_BYTES + 1)
    r = post(client, body)
    assert r.status_code == 413
    assert client.app.state.pull_calls == []


def test_oversized_content_length_is_rejected_without_reading_body(client):
    r = post(
        client,
        push_payload(),
        headers={"Content-Length": str(webhook.MAX_BODY_BYTES + 1)},
    )
    assert r.status_code == 413
    assert client.app.state.pull_calls == []


# --- the OAuth/bearer contract is unchanged ---------------------------------

def test_webhook_needs_no_bearer_token(client):
    """GitHub cannot send one; the signature is the credential."""
    assert "authorization" not in {k.lower() for k in client.headers}
    assert post(client, push_payload()).status_code == 200


def test_webhook_path_is_auth_exempt_and_routed():
    """Exempt path and mounted route must agree, or the transport serves it unauthed."""
    assert webhook.WEBHOOK_PATH in auth._AUTH_EXEMPT_PATHS
    assert webhook.WEBHOOK_PATH in [getattr(r, "path", None) for r in fresh_app().routes]


def test_mcp_transport_still_requires_auth(client):
    """Regression: adding an exempt path must not loosen the rest of the surface."""
    for method in ("get", "post"):
        assert getattr(client, method)("/").status_code == 401


def test_health_still_works(client):
    assert client.get("/health").json()["status"] == "ok"


def test_mount_path_cannot_collide_with_the_webhook(monkeypatch):
    """Mounting the vault transport on the webhook path would serve it unauthenticated."""
    for path in ("/webhooks", "/webhooks/github", "/webhooks/github/sub"):
        with pytest.raises(ValueError):
            config._validate_mcp_path(path)


# --- timeout configuration ---------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("25", 25.0), ("5", 5.0), ("30", 30.0), ("999", 30.0), ("0", 25.0), ("-1", 25.0), ("abc", 25.0), ("", 25.0)],
)
def test_timeout_is_parsed_and_clamped(monkeypatch, raw, expected):
    monkeypatch.setattr(config, "WEBHOOK_TIMEOUT", raw)
    assert config.webhook_timeout() == expected


def test_timeout_never_exceeds_the_cap(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_TIMEOUT", "600")
    assert config.webhook_timeout() <= config.WEBHOOK_TIMEOUT_MAX == 30.0


# --- pull_vault against real git repositories --------------------------------

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def vault_clone(tmp_path, monkeypatch):
    """A real vault clone tracking a real remote, wired up as config.VAULT_PATH."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    vault = tmp_path / "vault"

    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "note.md").write_text("first\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "first")
    git(seed, "push", "-u", "origin", "main")

    subprocess.run(["git", "clone", str(origin), str(vault)], check=True, capture_output=True)
    git(vault, "config", "user.email", "vault@example.com")
    git(vault, "config", "user.name", "Vault")

    monkeypatch.setattr(config, "VAULT_PATH", vault)
    monkeypatch.setattr(config, "WEBHOOK_TIMEOUT", "25")
    return {"vault": vault, "seed": seed}


def commit_upstream(seed, name, text):
    (seed / name).write_text(text)
    git(seed, "add", "-A")
    git(seed, "commit", "-m", f"add {name}")
    git(seed, "push", "origin", "main")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_pull_fast_forwards_and_counts_commits(vault_clone):
    commit_upstream(vault_clone["seed"], "second.md", "second\n")
    commit_upstream(vault_clone["seed"], "third.md", "third\n")

    assert webhook.pull_vault() == {"status": "pulled", "commits": 2}
    # The pull is only real if the files actually landed in the vault.
    assert (vault_clone["vault"] / "second.md").read_text() == "second\n"
    assert (vault_clone["vault"] / "third.md").read_text() == "third\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_pull_when_already_up_to_date_is_a_clean_noop(vault_clone):
    """Idempotence: a redelivered webhook must not error, just report nothing new."""
    commit_upstream(vault_clone["seed"], "second.md", "second\n")
    assert webhook.pull_vault() == {"status": "pulled", "commits": 2 - 1}

    for _ in range(3):
        assert webhook.pull_vault() == {"status": "pulled", "commits": 0}


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_pull_on_an_untouched_repo_reports_zero(vault_clone):
    assert webhook.pull_vault() == {"status": "pulled", "commits": 0}


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_diverged_worktree_fails_without_raising(vault_clone):
    """--ff-only must refuse rather than auto-merge; the handler still answers cleanly."""
    vault = vault_clone["vault"]
    (vault / "local.md").write_text("local only\n")
    git(vault, "add", "-A")
    git(vault, "commit", "-m", "local change")
    commit_upstream(vault_clone["seed"], "remote.md", "remote\n")

    result = webhook.pull_vault()
    assert result["status"] == "failed"
    # The local commit is untouched -- a failed pull must never discard vault work.
    assert (vault / "local.md").read_text() == "local only\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_pull_failure_never_leaks_the_remote_url(vault_clone):
    """The remote carries a PAT in this deployment; an unauthenticated caller must not see it."""
    vault = vault_clone["vault"]
    git(vault, "remote", "set-url", "origin", "https://token123@example.invalid/x.git")
    result = webhook.pull_vault()
    assert result["status"] == "failed"
    assert "token123" not in json.dumps(result)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_non_git_vault_fails_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    assert webhook.pull_vault()["status"] == "failed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_concurrent_delivery_is_skipped_not_queued(vault_clone, monkeypatch):
    """A second delivery while a pull runs returns immediately instead of racing git."""
    webhook._pull_lock.acquire()
    try:
        assert webhook.pull_vault() == {
            "status": "skipped",
            "reason": "pull already in progress",
        }
    finally:
        webhook._pull_lock.release()
    # The lock is released again afterwards, so normal pulls still work.
    assert webhook.pull_vault()["status"] == "pulled"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_end_to_end_signed_push_pulls_the_real_vault(vault_clone, monkeypatch):
    """Full path: signed delivery -> assembled app -> real git pull -> new file on disk."""
    monkeypatch.setattr(config, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "WEBHOOK_BRANCH", "main")
    monkeypatch.setattr(auth, "VAULT_MCP_TOKEN", "bearer-token-for-tests")
    commit_upstream(vault_clone["seed"], "from-webhook.md", "synced\n")

    with TestClient(fresh_app()) as c:
        r = post(c, push_payload())

    assert r.status_code == 200
    assert r.json() == {"status": "pulled", "commits": 1}
    assert (vault_clone["vault"] / "from-webhook.md").read_text() == "synced\n"
