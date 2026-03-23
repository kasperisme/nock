#!/usr/bin/env bash
# Deploy Notion CRM functions to Digital Ocean Functions.
#
# Prerequisites:
#   - doctl installed: https://docs.digitalocean.com/reference/doctl/how-to/install/
#   - doctl auth init (run once)
#   - A serverless namespace connected: doctl serverless connect
#   - .env file with secrets (copy from .env.example and fill in values)
#
# Usage:
#   ./deploy.sh              # deploy all functions
#   ./deploy.sh --dry-run    # validate project without deploying

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

if ! command -v doctl &>/dev/null; then
  echo "ERROR: doctl is not installed."
  echo "Install it from: https://docs.digitalocean.com/reference/doctl/how-to/install/"
  exit 1
fi

if ! doctl account get &>/dev/null; then
  echo "ERROR: doctl is not authenticated. Run: doctl auth init"
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: .env file not found at $ENV_FILE"
  echo "Copy .env.example to .env and fill in your secrets:"
  echo "  cp $SCRIPT_DIR/.env.example $SCRIPT_DIR/.env"
  exit 1
fi

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

echo "==> Building..."
bash "$SCRIPT_DIR/build.sh"

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

cd "$SCRIPT_DIR"

if [[ "${1:-}" == "--dry-run" ]]; then
  echo "==> Dry run — validating project (no deployment)..."
  doctl serverless deploy . --env "$ENV_FILE" --dry-run
  echo "Validation complete."
else
  echo "==> Deploying to Digital Ocean Functions (remote build)..."
  doctl serverless deploy . --env "$ENV_FILE" --remote-build
  echo ""
  echo "==> Deployment complete. Function URLs:"
  doctl serverless functions list --no-header 2>/dev/null | grep "notion-crm/crm" | awk '{print $1, $3}' || true
fi
