#!/usr/bin/env bash
# Единственная точка входа: ./run.sh <архив-датасета.zip>
set -euo pipefail
ARCHIVE="${1:?usage: ./run.sh <dataset.zip>}"
# Абсолютный путь до cd: архив 9 августа лежит там, куда его положили
# организаторы, а относительный путь после cd искался бы в корне репозитория.
case "$ARCHIVE" in
  /*) ;;
  *) ARCHIVE="$PWD/$ARCHIVE" ;;
esac
[ -f "$ARCHIVE" ] || { echo "архив не найден: $ARCHIVE" >&2; exit 1; }
cd "$(dirname "$0")"
uv sync --frozen --extra dev >/dev/null
exec uv run python solution/solve.py "$ARCHIVE"
