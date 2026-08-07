"""Пункт → спека (5.3): LLM читает договор, грамматика проверяет до исполнения.

quote обязателен: он и есть трейс, и по нему верификатор (и эвристика
лестницы) работают, не перечитывая PDF. Артефакт на диске хранит только
сырой ответ модели (covenants + алярмы извлечения) — грамматическая и
guard-проверка (_check) гоняются заново при каждом вызове extract_specs
с актуальными fact_keys, а не однократно при извлечении: подрезка фактов
после спек не должна требовать повторного похода к модели.
"""

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import llm
from dsl import Cmp, Doc, DslError, parse, validate, walk
from fallbacks import family_of
from guard import DATA_NOT_COMMANDS, sanitize_document, verify_quote
from stages import artifact
from taxonomy import LEAVES
from templates import match_signature, title_key

SPECS_STAGE_VERSION = 1
SCHEMA_VERSION = "specs-1"

# Порог семейного выброса (задача 23, п.5в): порог отличается от медианы
# порогов той же семьи метрики в этом прогоне на порядок и более.
_OUTLIER_FACTOR = Decimal(10)

_CLAUSE_NUM = re.compile(r"\d+(?:\.\d+)*")

SPECS_SCHEMA = {
    "type": "object",
    "properties": {
        "covenants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "quote": {"type": "string"},
                    "metric": {"type": "string"},
                    "direction": {"type": "string", "enum": ["max", "min"]},
                    "limit": {"type": "string"},
                    "trigger": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["clause", "quote", "metric", "direction", "limit", "trigger", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["covenants"],
    "additionalProperties": False,
}

SPECS_PROMPT = """Ниже — кредитный договор. Найди в нём ВСЕ финансовые ковенанты
(обязательства с числовым порогом) и для каждого выдай:
- clause: номер пункта, под которым ковенант напечатан в договоре;
- quote: дословная цитата пункта;
- metric: показатель на DSL (грамматика ниже);
- direction: max — показатель не должен превышать порог, min — не должен быть ниже;
- limit: порог строкой (доли — числом: 5% => 0.05; кратности: 2.5x => 2.5);
- trigger: если тест применяется только при условии — условие как сравнение
  gt/ge/lt/le двух DSL-выражений, иначе null. trigger заполняется ТОЛЬКО когда
  применение теста зависит от значения показателя (пример: «если поступления
  по финансированию превышают пороговую сумму» — springing-ковенант). Срок действия
  договора, отчётный период, дата — НЕ триггеры, для них trigger: null;
- confidence: уверенность 0..1.

Грамматика DSL:
  expr    := agg(category, sign, filters?) | doc(key) | ratio(a,b) | sub(a,b)
           | add(a...) | max(a...) | min(a...) | const(x)
  sign    := out | in | net   (out — расходы по модулю, дефолт для расходов;
                               net — с неттингом сторно, только если договор явно
                               требует вычитать возвраты/сторно из расхода;
                               in — поступления)
  filters := period(YYYY-MM-DD,YYYY-MM-DD) | quarter(n)
           | counterparty_in(related_parties | unrestricted_subsidiaries | ['Имя', ...])
           | txn_in(['TXN', ...]) | min_amount(x) | desc_contains('строка')
  ВНИМАНИЕ: period()/quarter() — это ОТЧЁТНЫЙ период метрики (за какой интервал
  считать), НЕ исключение операций. Исключение конкретной операции по
  документальному решению (отсечение периода, переход рисков) НЕ выражается
  фильтром — оно приходит из фактов досье (excluded_txns) и применяется до DSL.
Категории: {categories}
Роллапы: OPEX_TOTAL (все операционные расходы), ALL (все категории).
EBITDA выражай через sub(agg(REVENUE, in), agg(<роллап>, out)) и выбирай роллап
по тексту договора: OPEX_TOTAL — если договор понимает под операционными
расходами все статьи, OTHER_OPEX — если только прочие/эксплуатационные;
цитируй формулировку, из которой следует выбор.
Если число берётся из документа, а не из леджера (например консолидированный
показатель группы или зафиксированное обязательство) — используй doc(ключ);
доступные ключи: {fact_keys}; если нужного ключа нет — придумай осмысленный
snake_case ключ, он будет извлечён отдельно.

<agreement>
{text}
</agreement>"""


def _normalize_clause(raw: str) -> tuple[str, bool]:
    """Номер пункта из ответа модели то голый, то с префиксом «Пункт»/«п.»/
    «Article» — ключ ячейки берётся только из цифровой части.

    Второй элемент — found: цифровая часть вообще нашлась в ответе модели
    (не «клауза совпала с шаблоном» — совпадение с шаблоном не отсюда)."""
    m = _CLAUSE_NUM.search(raw)
    if m:
        return m.group(), True
    return raw, False


def _strip_trailing_zeros(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def _limit_forms(limit: str) -> set[str]:
    """Цифровые формы порога для поиска внутри цитаты (два знака, один, целое),
    а для долей — ещё и процентная форма (доля как число и процент)."""
    try:
        d = Decimal(limit)
    except InvalidOperation:
        return {limit}
    plain = format(d, "f")
    forms = {limit, plain, _strip_trailing_zeros(plain)}
    if 0 < d <= 1:
        pct = format(d * 100, "f")
        forms.add(f"{_strip_trailing_zeros(pct)}%")
    return forms


_THOUSANDS_SEP = re.compile(r"(?<=\d)[,\x20\xa0\u202f](?=\d{3}(?:\D|$))")


def _degroup_thousands(text: str) -> str:
    """Снимает разделители тысяч (запятая, неразрывный/узкий неразрывный
    пробел) между группами цифр в числовых значениях. Цитата уже
    прошла verify_quote (доказанно реальный текст договора) — здесь только
    сверяем цифровую форму порога, снимать разделители безопасно."""
    return _THOUSANDS_SEP.sub("", text)


def _limit_in_quote(limit: str, quote: str) -> bool:
    degrouped = _degroup_thousands(quote)
    return any(form in quote or form in degrouped for form in _limit_forms(limit))


def _check(sp: dict, fact_keys: set[str], agreement_text: str) -> tuple[dict, object | None]:
    out = {
        **sp,
        "valid": False,
        "errors": [],
        "template": None,
        "missing_doc_keys": [],
        "trigger_discarded": False,
        "title_key": title_key(sp["quote"]),
    }
    try:
        node = parse(sp["metric"])
    except DslError as exc:
        out["errors"].append(f"metric: {exc}")
        return out, None
    missing = sorted({n.key for n in walk(node) if isinstance(n, Doc)} - fact_keys)
    out["missing_doc_keys"] = missing
    errors = [e for e in validate(node, fact_keys) if "doc-ключ" not in e]

    # Порог — самая чувствительная точка prompt-injection: подменённый limit
    # тихо переворачивает вердикт, поэтому и цитата, и порог обязаны быть
    # верифицируемы в исходном тексте договора, а не просто правдоподобны.
    if not verify_quote(sp["quote"], agreement_text):
        errors.append("quote_unverified")
    elif not _limit_in_quote(sp["limit"], sp["quote"]):
        errors.append("limit_not_in_quote")

    trig_value = sp["trigger"]
    if trig_value:
        discard = False
        try:
            trig_node = parse(trig_value)
            trig_errors = [e for e in validate(trig_node, fact_keys) if "doc-ключ" not in e]
            discard = bool(trig_errors) or not isinstance(trig_node, Cmp)
        except DslError:
            discard = True
        if discard:
            # Кривой/ложный триггер (например «период действия договора») не
            # должен стоить ячейку: метрика и порог валидны сами по себе.
            trig_value = None
            out["trigger_discarded"] = True
    out["trigger"] = trig_value

    out["errors"] = errors
    out["valid"] = not errors and not missing
    out["template"] = match_signature(node) if out["valid"] else None
    return out, node


def _median(values: list[Decimal]) -> Decimal:
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2


def _flag_outliers(clauses: dict, parsed_nodes: dict[str, object], alarms: list[dict]) -> None:
    """Порог, отличающийся на порядок и более от медианы своей семьи метрики
    в этом прогоне, не роняет ячейку — только помечает её на глаза.

    parsed_nodes — узлы, уже распарсенные в _check; повторный parse() тут не нужен."""
    parsed: dict[str, tuple[object, Decimal]] = {}
    for key in sorted(parsed_nodes):
        try:
            limit = Decimal(clauses[key]["limit"])
        except InvalidOperation:
            continue
        parsed[key] = (parsed_nodes[key], limit)
    by_family: dict[str, list[Decimal]] = {}
    for node, limit in parsed.values():
        fam = family_of(node, limit)
        if fam is not None:
            by_family.setdefault(fam, []).append(limit)
    for key in sorted(parsed):
        node, limit = parsed[key]
        fam = family_of(node, limit)
        peers = by_family.get(fam, []) if fam is not None else []
        if len(peers) < 2 or limit == 0:
            continue
        med = _median(peers)
        if med == 0:
            continue
        ratio = limit / med if limit > med else med / limit
        if ratio >= _OUTLIER_FACTOR:
            alarms.append({"kind": "limit_outlier", "clause": key})


def extract_specs(wd: Path, dossier_art: dict, fact_keys: set[str]) -> dict:
    acc = dossier_art["account_id"]
    agreements = [d for d in dossier_art["docs"] if d["doc_type"] == "agreement"]

    def build() -> dict:
        if not agreements:
            return {"covenants": [], "alarms": [{"kind": "no_agreement", "account": acc}]}
        text = sanitize_document(agreements[0]["text"])
        prompt = (
            DATA_NOT_COMMANDS
            + "\n\n"
            + SPECS_PROMPT.format(
                categories=", ".join(sorted(LEAVES)),
                fact_keys=", ".join(sorted(fact_keys)) or "(пока нет)",
                text=text,
            )
        )
        try:
            raw = llm.call(prompt, SPECS_SCHEMA, SCHEMA_VERSION, max_tokens=16000)
        except llm.SchemaRejected as exc:
            return {"covenants": [], "alarms": [{"kind": "specs_extraction_failed", "error": str(exc)}]}
        return {"covenants": raw["covenants"], "alarms": []}

    raw_art = artifact(wd / "specs" / f"{acc}.json", SPECS_STAGE_VERSION, build)
    agreement_text = sanitize_document(agreements[0]["text"]) if agreements else ""

    alarms = list(raw_art["alarms"])
    clauses: dict[str, dict] = {}
    parsed_nodes: dict[str, object] = {}
    for sp in raw_art["covenants"]:
        clause_key, found = _normalize_clause(sp["clause"])
        if not found:
            alarms.append({"kind": "clause_unmatched", "clause": sp["clause"]})
        if clause_key in clauses:
            alarms.append({"kind": "duplicate_clause", "clause": clause_key})
            continue
        checked, node = _check({**sp, "clause": clause_key}, fact_keys, agreement_text)
        if checked.pop("trigger_discarded", False):
            alarms.append({"kind": "trigger_discarded", "clause": clause_key})
        clauses[clause_key] = checked
        if node is not None:
            parsed_nodes[clause_key] = node
        if not checked["valid"] and not checked["missing_doc_keys"]:
            alarms.append({"kind": "invalid_spec", "clause": clause_key, "errors": checked["errors"]})

    _flag_outliers(clauses, parsed_nodes, alarms)
    return {"clauses": clauses, "alarms": alarms}
