# Deployment: GitHub push webhook on Railway

How to run this server on Railway with the vault kept in a private GitHub repository, and
have GitHub tell the server to `git pull` the instant the vault changes.

Without the webhook, the server's copy of the vault only refreshes when something pulls it.
Commits pushed from another machine (Obsidian Git, an n8n workflow writing through the
GitHub API, the GitHub web editor) stay invisible to the MCP tools until then. With the
webhook, a push to the watched branch triggers a pull within a second.

**Prerequisites:** the service already clones the vault repo into `VAULT_PATH` at startup
and can authenticate to it (a PAT in `GITHUB_TOKEN`, used by `start.sh`). This guide does
not change that — it only adds the webhook on top.

---

## Section A — Deploy on Railway

### A1. Point the Railway service at your fork

The webhook lives in this fork, so the service has to build from it.

1. Open the Railway project and select the service running the MCP server.
2. **Settings → Source**. If it is connected to `jimprosser/obsidian-web-mcp`, disconnect
   it and connect `<your-user>/obsidian-web-mcp` instead.
3. Set **Branch** to `main` — after the webhook PR is merged into the fork's `main`.
   To test before merging, set the branch to `feature/github-webhook`.
4. Leave the start command as-is (`./start.sh`, or whatever the service already uses).

If the service is already building from your fork, there is nothing to change here.

### A2. Generate the webhook secret

Run this locally and keep the output — you will paste the **same value** into Railway
(step A3) and GitHub (step B2). It is shown only once; store it in your password manager.

```bash
openssl rand -hex 32
```

This is the only credential protecting an endpoint that runs a subprocess, so do not reuse
an existing token and do not use a short or memorable value.

### A3. Add the environment variables in Railway

**Variables** tab → **New Variable**, for each of:

| Variable | Value | Required |
| -------- | ----- | -------- |
| `WEBHOOK_SECRET` | the value from A2 | Yes — without it the endpoint answers `503` |
| `WEBHOOK_BRANCH` | `main` | No (defaults to `main`) |
| `WEBHOOK_TIMEOUT` | `25` | No (defaults to `25`, capped at `30`) |

Do not change `VAULT_PATH`, `GITHUB_TOKEN`, `VAULT_MCP_TOKEN`, or any `VAULT_OAUTH_*`
variable — the webhook does not touch the vault-clone or OAuth configuration.

### A4. Redeploy

Saving a variable normally triggers a redeploy. If it does not, use **Deployments → Deploy**
(or **Redeploy** on the latest deployment). Wait for the build to go green.

### A5. Verify the service still works

Health check — this must still answer exactly as before:

```bash
curl -s https://<your-service>.up.railway.app/health
```

Expected: `{"status":"ok","audit":{"enabled":false}}` (the `audit` value depends on your
config; the `"status":"ok"` is the part that matters).

Confirm the MCP transport is still authenticated — adding the webhook must not have opened
anything up. This must return **401**:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<your-service>.up.railway.app/
```

Confirm the webhook is mounted and rejecting unsigned callers. This must return **401**
(not `404`, not `200`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'X-GitHub-Event: push' -H 'Content-Type: application/json' \
  -d '{"ref":"refs/heads/main"}' \
  https://<your-service>.up.railway.app/webhooks/github
```

If you get `503`, `WEBHOOK_SECRET` did not reach the container — recheck A3 and redeploy.
If you get `404`, the service is still building the old code — recheck A1.

Finally, reconnect nothing: existing Claude/MCP connections keep working, since the OAuth
configuration is untouched.

---

## Section B — Configure the webhook on GitHub

This is done on the **vault** repository (the one holding your notes), not on the fork of
the server.

### B1. Open the webhook form

Go to the vault repo → **Settings** → **Webhooks** → **Add webhook**.

### B2. Fill it in

| Field | Value |
| ----- | ----- |
| **Payload URL** | `https://<your-service>.up.railway.app/webhooks/github` |
| **Content type** | `application/json` |
| **Secret** | the exact value from A2 |
| **SSL verification** | **Enable SSL verification** |
| **Which events?** | **Let me select individual events** → check **Pushes** only |
| **Active** | checked |

`application/json` matters: with `application/x-www-form-urlencoded` GitHub sends the
payload as a form field and the JSON parse fails, so every delivery is skipped.

Click **Add webhook**.

### B3. Check the ping

GitHub immediately sends a `ping` event. Open the webhook → **Recent Deliveries** → the
`ping` entry. Expect **200** with:

```json
{"status": "skipped", "reason": "event ping is not push"}
```

That response is the goal: it proves the signature verified (an invalid one returns `401`)
and that non-push events are correctly ignored.

### B4. Test with a real push

Commit anything to the watched branch:

```bash
git commit --allow-empty -m "test: webhook" && git push origin main
```

In **Recent Deliveries**, the `push` entry should be **200** with:

```json
{"status": "pulled", "commits": 1}
```

Then confirm the content actually landed: ask Claude to read a note you just changed, or
check the Railway deploy logs for the `[webhook]` lines:

```
[webhook] push to main, pulling vault
[webhook] pulled 1 commit(s), HEAD now a1b2c3d4
```

### B5. Reading the outcomes

| Delivery result | Meaning | Fix |
| --------------- | ------- | --- |
| `200 {"status":"pulled","commits":N}` | Working | — |
| `200 {"status":"pulled","commits":0}` | Already current (a redelivery, or another sync got there first) | Normal |
| `200 {"status":"skipped","reason":"ref ... is not ..."}` | Push was to another branch | Set `WEBHOOK_BRANCH` if your default branch is not `main` |
| `200 {"status":"failed","reason":"git pull failed"}` | Usually a diverged worktree | See "Diverged worktree" below |
| `401 {"reason":"invalid signature"}` | Secret mismatch | Re-paste the same value in Railway and GitHub |
| `503 {"reason":"webhook not configured"}` | `WEBHOOK_SECRET` not set in the container | Recheck A3, redeploy |
| `404` | Old code deployed | Recheck A1 |

---

## Testing the endpoint by hand

To verify the endpoint without pushing anything, sign a payload yourself. Replace
`WEBHOOK_SECRET` with your real secret and `<your-service>` with your host:

```bash
SECRET='paste-your-secret-here'
URL='https://<your-service>.up.railway.app/webhooks/github'
BODY='{"ref":"refs/heads/main","repository":{"full_name":"owner/vault"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
curl -sS -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: $SIG" \
  --data-binary "$BODY" -w '\n%{http_code}\n'
```

Expected: `{"status": "pulled", "commits": 0}` and `200` when the vault is already current.

The signature covers the **exact bytes** of the body, which is why `--data-binary` is used
and why `$BODY` is signed with `printf '%s'` (no trailing newline). Changing one byte of
the body without re-signing gives `401` — a useful check that verification really is on:

```bash
curl -sS -X POST "$URL" \
  -H 'Content-Type: application/json' -H 'X-GitHub-Event: push' \
  -H "X-Hub-Signature-256: $SIG" \
  --data-binary '{"ref":"refs/heads/main","tampered":true}' -w '\n%{http_code}\n'
```

---

## Operational notes

**Diverged worktree.** The pull is `--ff-only`, so it refuses to merge. If the server has
local commits the remote does not (this deployment's `start.sh` auto-sync loop commits vault
changes on its own), the pull fails and the delivery reports `"failed"`. This is
deliberate — the alternative is an auto-merge silently rewriting vault files. Nothing is
lost; the auto-sync loop's `git pull --rebase` reconciles on its next cycle. To resolve it
immediately, redeploy the service.

**The webhook complements the sync loop, it does not replace it.** `start.sh` still pushes
locally-made changes on its interval. The webhook only adds the inbound direction, making
remote changes visible in seconds instead of minutes.

**GitHub's 10-second timeout.** A pull on a normal vault takes well under a second, so
deliveries land comfortably inside it. A first-time or very large fetch can exceed 10s;
GitHub then marks the delivery as timed out, but the pull still completes on the server
(the subprocess timeout is `WEBHOOK_TIMEOUT`, up to 30s). Check the `[webhook]` log lines
before assuming a timed-out delivery did nothing. Redelivering is always safe.

**Rotating the secret.** Set the new value in Railway, redeploy, then update the secret in
the GitHub webhook. Deliveries between the two steps fail with `401`; redeliver them from
the **Recent Deliveries** tab afterwards.

**Turning the webhook off.** Remove `WEBHOOK_SECRET` and redeploy. The route stays mounted
but fails closed with `503` and never runs git.
