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

echo "--- DEBUG: locating uv / vault-mcp ---"
echo "PATH=$PATH"
which uv 2>/dev/null || echo "uv not in PATH"
find / -maxdepth 4 \( -iname "vault-mcp" -o -iname "uv" \) 2>/dev/null | grep -v -e /proc -e "$VAULT_PATH"
echo "--- END DEBUG ---"

cd /app

if command -v uv >/dev/null 2>&1; then
  echo "Using: uv run vault-mcp"
  exec uv run vault-mcp
elif [ -x /app/.venv/bin/vault-mcp ]; then
  echo "Using: /app/.venv/bin/vault-mcp"
  exec /app/.venv/bin/vault-mcp
elif [ -x /app/venv/bin/vault-mcp ]; then
  echo "Using: /app/venv/bin/vault-mcp"
  exec /app/venv/bin/vault-mcp
else
  echo "vault-mcp introuvable — voir le DEBUG ci-dessus. Conteneur maintenu en vie pour lecture des logs."
  sleep 3600
fi
