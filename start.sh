#!/bin/bash
set -e

export PATH="/mise/shims:/root/.local/bin:/root/.cargo/bin:$PATH"

REPO_URL="https://${GITHUB_TOKEN}@github.com/teuwwaiii-prog/${GITHUB_REPO_NAME}.git"

if [ ! -d "$VAULT_PATH/.git" ]; then
  echo "Cloning $GITHUB_REPO_NAME into $VAULT_PATH..."
  git clone "$REPO_URL" "$VAULT_PATH"
fi

cd "$VAULT_PATH"
git config user.email "railway@sync"
git config user.name "Railway Sync"

sync_loop() {
  while true; do
    sleep 300
    cd "$VAULT_PATH"
    git add -A
    git diff --cached --quiet || git commit -m "Railway auto-sync $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git pull --rebase origin main || true
    git push origin main || true
  done
}
sync_loop &

exec uv run vault-mcp
