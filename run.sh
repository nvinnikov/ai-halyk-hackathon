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
# Реквизиты дня запуска (API-ключи, LLM_PROVIDER, TEAM_NAME/CONTACT_EMAIL/
# MODEL_NAME) — из .env, если он есть. Код сам .env не читает (anthropic-
# клиент и solve.submission_meta берут из окружения процесса) — sourcing
# здесь, чтобы 9 августа под трёхчасовым таймером не зависеть от того, что
# `source .env` не забыли выполнить руками перед вызовом. В CI .env не
# создаётся — переменные приходят из окружения раннера, ветка no-op.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
uv sync --frozen --extra dev >/dev/null
exec uv run python solution/solve.py "$ARCHIVE"
