#!/usr/bin/env bash
# Одна команда на боевое окно: архив → sanity → прогон → снапшот отправки.
#
#   ./go.sh /путь/к/приватному.zip
#
# Смысл — убрать из окна ручные шаги и решения, которые можно принять заранее.
# Скрипт НЕ добавляет логики поверх пайплайна: он делает ровно то же, что ранбук,
# в том же порядке. Всё, что он добавляет, — порядок, логи и чтение вывода.
#
# Зовём НЕ через make, и это две разные причины, обе всплыли на репетиции:
#
# 1. rtk-хук переписывает `make solve` в `rtk make solve`, и от лога прогона
#    остаётся три строки вместо всего вывода — алярмы в них не видны, а именно
#    по ним принимают решения после прогона. Внутренние вызовы скрипта хук не
#    трогает, поэтому здесь прямые команды.
# 2. Гейт `require-private-archive` сверяет архив С ПУБЛИЧНЫМ ФАЙЛОМ на диске
#    (`cmp`), а в теневом ворктри публичного архива нет — `cmp` считает это
#    расхождением и пропускает что угодно, включая публичный набор. Защита
#    здесь другая и надёжнее: стоп-строка sanity по dataset_hash, которая не
#    зависит от наличия файлов.
#
# Полный лог каждого шага пишется в out/window-<штамп>/, включая stdout прогона:
# разбирать run-report потом надо по нему, а не по прокрутке терминала.
set -uo pipefail
cd "$(dirname "$0")"

ARCHIVE="${1:-}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mСТОП: %s\033[0m\n' "$*" >&2; exit 1; }

if [ -z "$ARCHIVE" ]; then
  echo "usage: ./go.sh <архив-приватного-датасета.zip>" >&2
  echo >&2
  echo "Свежие архивы в ~/Downloads (проверьте, что берёте тот):" >&2
  ls -t "$HOME/Downloads"/*.zip 2>/dev/null | head -3 | sed 's/^/  /' >&2
  exit 2
fi
[ -f "$ARCHIVE" ] || die "архив не найден: $ARCHIVE"
case "$ARCHIVE" in /*) ;; *) ARCHIVE="$PWD/$ARCHIVE" ;; esac

STAMP=$(date +%Y%m%d-%H%M%S)
LOGDIR="out/window-$STAMP"
mkdir -p "$LOGDIR"
echo "лог окна: $LOGDIR"

# --- 0. Предполётное: то, что дешевле проверить сейчас, чем узнать в прогоне --
say "0/4 предполётная проверка"
[ -f .env ] || die ".env отсутствует — без ключей прогон пойдёт вслепую (см. .env.example)"
missing=""
for v in GEMINI_API_KEY LLM_PROVIDER TEAM_NAME CONTACT_EMAIL; do
  grep -qE "^${v}=.+" .env || missing="$missing $v"
done
[ -n "$missing" ] && echo "  ВНИМАНИЕ: в .env пусто или нет:$missing"
cass=$(ls eval/cassette 2>/dev/null | wc -l | tr -d ' ')
echo "  кассета: $cass записей (ожидается ~655; на приватном наборе попаданий не будет, это нормально)"
echo "  .env: $(cut -d= -f1 .env | tr '\n' ' ')"

# --- 1. Sanity: единственная проверка, которая обязана остановить -------------
say "1/4 sanity по архиву"
uv sync --frozen --extra dev >/dev/null 2>&1 || echo "  (uv sync не прошёл — продолжаем, run.sh повторит)"
if ! uv run python solution/sanity.py "$ARCHIVE" 2>&1 | tee "$LOGDIR/sanity.log"; then
  die "sanity упал — смотрите $LOGDIR/sanity.log"
fi
if grep -q "СОВПАЛ С ПУБЛИЧНЫМ НАБОРОМ" "$LOGDIR/sanity.log"; then
  die "это ПУБЛИЧНЫЙ архив, а не приватный. Взяли не тот файл."
fi
echo
echo "  --- что важно из sanity ---"
grep -E "^(dataset_hash|pdf_count|blind_pages|targets|doc_types):" "$LOGDIR/sanity.log" | sed 's/^/  /'
grep -E "^DIFF " "$LOGDIR/sanity.log" | sed 's/^/  /' || true
echo "  (fallback_rate и stage_alarms в выводе выше — от ПРОШЛОГО прогона на этом"
echo "   work/<hash>, а не про этот архив: sanity зовётся до solve. На свежем"
echo "   приватном архиве там будет None — это норма, а не пустой прогон.)"
echo
echo "  pdf_count и doc_types.unrouted задают время: второй проход маршрутизации"
echo "  зовёт META по каждому непривязанному документу."

# --- 2. Прогон ----------------------------------------------------------------
say "2/4 боевой прогон (ожидание 60–75 минут, не запускайте ничего параллельно)"
echo "  параллельный pytest или второй прогон по тому же work/<hash> ОТРАВЛЯЕТ артефакты."
start=$(date +%s)
./run.sh "$ARCHIVE" 2>&1 | tee "$LOGDIR/run.log"
rc=${PIPESTATUS[0]}
echo "  прогон занял $((($(date +%s) - start) / 60)) мин, код возврата $rc"
[ "$rc" -ne 0 ] && echo "  ВНИМАНИЕ: прогон завершился ненулевым кодом — submission всё равно валиден, см. ниже"

# --- 3. Чтение run-report за вас ---------------------------------------------
say "3/4 итоги прогона"
# Сверка происхождения — до всякой статистики. Если прогон упал ДО записи своего
# run-report, в out/ лежит отчёт ПРОШЛОГО прогона (на репетициях — публичного), и
# statistics ниже описывала бы чужой результат. submit такое блокирует по
# is_public_dataset, но под таймером блокировку пробивают через SUBMIT_FORCE=1 —
# и отправляют публичный набор. Поэтому здесь громкий стоп, а не подсказка.
want_hash=$(grep -m1 "^dataset_hash:" "$LOGDIR/sanity.log" | awk '{print $2}')
got_hash=$(uv run python -c "
import json
from pathlib import Path
p = Path('out/run-report.json')
print(json.loads(p.read_text()).get('dataset_hash', '') if p.exists() else '')
" 2>/dev/null)
if [ -n "$want_hash" ] && [ "$want_hash" != "$got_hash" ]; then
  echo "  dataset_hash архива: $want_hash"
  echo "  dataset_hash в out/run-report.json: ${got_hash:-<нет файла>}"
  die "прогон не оставил своего run-report — в out/ лежит отчёт ДРУГОГО прогона.
       Снапшот НЕ снят намеренно: SUBMIT_FORCE=1 здесь отправил бы чужой результат.
       Смотрите $LOGDIR/run.log, ищите причину падения, затем перезапустите ./go.sh."
fi
uv run python - "$LOGDIR" <<'PY' 2>&1 | tee "$LOGDIR/summary.txt"
import json, sys
from pathlib import Path

sub = Path("out/submission.json")
if sub.exists():
    answers = json.loads(sub.read_text()).get("answers", {})
    cells = [c for s in answers.values() for c in s.values()]
    filled = sum(1 for c in cells if c.get("status") is not None)
    print(f"submission: {filled}/{len(cells)} ячеек заполнено")
else:
    print("submission: ФАЙЛА НЕТ — это единственный настоящий провал окна")

rep = Path("out/run-report.json")
if not rep.exists():
    print("run-report отсутствует — читайте лог прогона руками")
    sys.exit(0)
r = json.loads(rep.read_text())
print("tier_breakdown:", r.get("tier_breakdown"))
print("budget:", r.get("budget"))
print("duration_s:", r.get("duration_s"))

alarms = r.get("alarm_counts", {}) or {}
# Порядок — по тому, в каком читают в окне: сначала то, что меняет ответ.
first = [
    "heading_divergence_changed_answer",
    "shadow_failed",
    "issuer_extraction_failed",
    "group_doc_attached",
    "group_capex_movement_incomplete",
    "group_capex_currency_unnamed",
    "group_capex_scale_unnamed",
    "group_capex_conflict",
    "heuristic_family_mismatch",
    "extracted_inputs_failed",
]
print("\nсмотреть в этом порядке:")
for k in first:
    if k in alarms:
        print(f"  {k}: {alarms[k]}")
rest = {k: v for k, v in sorted(alarms.items()) if k not in first}
if rest:
    print("\nостальные алярмы:", json.dumps(rest, ensure_ascii=False))
PY

# --- 4. Снапшот отправки ------------------------------------------------------
say "4/4 снапшот отправки"
uv run python solution/submit.py 2>&1 | tee "$LOGDIR/submit.log"

say "готово"
echo "  отправлять: out/submission.json (снапшот — out/submission-<N>.json)"
echo "  логи окна:  $LOGDIR"
echo
echo "  Дальше по ранбуку: docs/ops/runbook-2026-08-09.md, раздел «После прогона»."
echo "  Правило окна: код НЕ чинить. Перезапуск без правки даст тот же результат (кэш)."
