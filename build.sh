#!/usr/bin/env bash
# Copies shared lib/ files into each function directory before deployment.
# Digital Ocean Functions bundles each function directory independently,
# so shared code must be present inside each function folder.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"

FUNCTIONS=(
  "packages/notion-crm/crm"
)

echo "Building DO Functions — cleaning and copying shared lib..."

for func_rel in "${FUNCTIONS[@]}"; do
  func_dir="$SCRIPT_DIR/$func_rel"
  rm -rf "$func_dir/__deployer__.zip" "$func_dir/virtualenv"
  echo "  -> $func_rel/"
  cp "$LIB_DIR/notion_client.py" "$func_dir/notion_client.py"
  cp "$LIB_DIR/crm_logger.py"    "$func_dir/crm_logger.py"
done

echo "Build complete."
