#!/usr/bin/env bash
# Единственная точка входа: ./run.sh <архив-датасета.zip>
set -euo pipefail
ARCHIVE="${1:?usage: ./run.sh <dataset.zip>}"
cd "$(dirname "$0")"
uv sync --frozen --extra dev >/dev/null
exec uv run python solution/solve.py "$ARCHIVE"
