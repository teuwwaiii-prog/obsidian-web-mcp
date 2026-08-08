#!/bin/bash
set +e

export PATH="/mise/shims:/root/.local/bin:/root/.cargo/bin:$PATH"

REPO_URL="https://${GITHUB_TOKEN}@github.com/teuwwaiii-prog/${GITHUB_REPO_NAME}.git"

if [ ! -d "$VAULT_PATH/.git" ]; then
  echo "Cloning $GITHUB_REPO_NAME into $VAULT_PATH..."
  git clone "$REPO_URL" "$VAULT_PATH"
fi

cd "$VAULT_PATH" || exit 1
git config user.email "railway@sync"
git config user.name "Railway Sync"

sync_loop() {
  while true; do
    sleep 300
    cd "$VAULT_PATH" || continue
    git add -A
    git diff --cached --quiet || git commit -m "Railway auto-sync $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git pull --rebase origin main
    git push origin main
  done
}
sync_loop &

echo "--- DEBUG: environment ---"
echo "PATH=$PATH"
echo "whoami: $(whoami)"
echo "--- ls /app ---"
ls -la /app 2>&1
echo "--- searching for uv and vault-mcp binaries ---"
find / -xdev \( -iname "uv" -o -iname "vault-mcp" \) 2>/dev/null
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
  echo "vault-mcp introuvable — voir DEBUG ci-dessus. Conteneur maintenu en vie."
  sleep 3600
fi
