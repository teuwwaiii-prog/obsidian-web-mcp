#!/bin/bash
set +e

export PATH="/root/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="/root/.local/bin:$PATH"
fi

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

cd /app
echo "Installing project dependencies..."
uv sync

echo "Starting vault-mcp..."
exec uv run vault-mcp
