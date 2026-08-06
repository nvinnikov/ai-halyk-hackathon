"""Клиент Anthropic: content-addressed кэш, ретраи, потолок бюджета.

Ключ кэша = sha256(model + prompt + json_schema + schema_version) — раздел 3
спеки. Кэш общий между наборами, никогда не инвалидируется по времени; в кэш
попадает только успешный ответ, прошедший валидацию схемы. Джиттер backoff —
из ключа кэша, а не из random(): иначе ломается воспроизводимость.

Порядок чтения: сначала кассета eval/cassette/<key>.json (регрессионный
забор для публичного архива — правка промпта не сломает извлечение молча;
на приватном датасете кассета не даёт попаданий, это ожидаемо), затем
work/llm_cache/<key>.json, затем сеть. При LLM_OFFLINE=1 промах и кассеты, и
кэша — это CassetteMiss, а не сетевой вызов.

Ручное редактирование содержимого кэша запрещено: это единственный способ
получить submission, который невозможно воспроизвести.
"""

import hashlib
import json
import os
import threading
import time

import anthropic
import jsonschema

from util import ROOT, stable_json

MODEL = "claude-haiku-4-5-20251001"
CACHE = ROOT / "work" / "llm_cache"
CASSETTE = ROOT / "eval" / "cassette"
# Суммарный потолок сетевых обращений на один call() — раздел 6 спеки.
# Общий бюджет: и ретраи на транзиентные ошибки, и единственный
# max_tokens-повтор расходуют попытки из одного и того же счётчика, а не
# каждый по своему циклу до MAX_ATTEMPTS (иначе один call() мог бы сделать
# до 2×MAX_ATTEMPTS запросов).
MAX_ATTEMPTS = 4
DEFAULT_MAX_TOKENS = 8000
# стандартный прайс Sonnet 5; до 2026-08-31 действует вводный $2/$10 за млн —
# учёт консервативен в 1.5 раза
PRICE_IN, PRICE_OUT = 3e-6, 15e-6

RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
)

_budget = {"spent_usd": 0.0, "ceiling_usd": float(os.environ.get("LLM_BUDGET_USD", "50"))}
_budget_lock = threading.Lock()
_client: anthropic.Anthropic | None = None


class BudgetExhausted(Exception):
    """Потолок стоимости прогона: дальше — фолбэки из уже посчитанного."""


class SchemaRejected(Exception):
    """Ответ модели не прошёл схему: не сетевая проблема, ретрай не чинит."""


class CassetteMiss(Exception):
    """LLM_OFFLINE=1, а ни кассета, ни work-кэш не содержат этот ключ."""


def budget_state() -> dict:
    return dict(_budget)


def cache_key(model: str, blocks: list, schema: dict, schema_version: str) -> str:
    payload = stable_json({"model": model, "prompt": blocks, "schema": schema, "v": schema_version})
    return hashlib.sha256(payload.encode()).hexdigest()


def _create(**kwargs):
    """Единственная точка обращения к API — подменяется в тестах."""
    global _client
    if _client is None:
        # max_retries=0: ретраи — наша забота (MAX_ATTEMPTS ниже), иначе
        # 4 ручные попытки × 2 SDK-ретрая = до 12 запросов на один call().
        _client = anthropic.Anthropic(max_retries=0)
    return _client.messages.create(**kwargs)


def _build_blocks(prompt: str, document_b64: str | None) -> list:
    """Инструкции — первым блоком (кэшируется), документ — последним."""
    blocks: list = [{"type": "text", "text": prompt}]
    if document_b64:
        blocks.append(
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": document_b64},
            }
        )
    return blocks


def _with_cache_control(blocks: list) -> list:
    """Копия blocks с ephemeral cache_control на блоке инструкций.

    Отдельно от blocks, которые идут в cache_key: пометка кэша Anthropic не
    меняет содержание промпта, поэтому не должна менять ключ нашего кэша.
    """
    first = dict(blocks[0], cache_control={"type": "ephemeral"})
    return [first, *blocks[1:]]


def _charge(usage) -> None:
    cost = usage.input_tokens * PRICE_IN + usage.output_tokens * PRICE_OUT
    with _budget_lock:
        _budget["spent_usd"] += cost


def _request(blocks: list, schema: dict, max_tokens: int, delay: float, attempts_left: int):
    """Один раунд до первого ответа: ретраит транзиентные ошибки в пределах
    attempts_left — общего на весь call() бюджета попыток (см. MAX_ATTEMPTS).

    Возвращает (resp, attempts_left) — остаток бюджета передаётся вызывающей
    стороне, чтобы max_tokens-повтор в call() тратил попытки из того же
    счётчика, а не заводил себе отдельный цикл до MAX_ATTEMPTS.
    """
    last: Exception | None = None
    while attempts_left > 0:
        attempt_no = MAX_ATTEMPTS - attempts_left
        attempts_left -= 1
        try:
            resp = _create(
                model=MODEL,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": blocks}],
                tools=[
                    {
                        "name": "emit",
                        "description": "Верни результат строго по схеме.",
                        "input_schema": schema,
                        "strict": True,
                    }
                ],
                tool_choice={"type": "tool", "name": "emit"},
            )
        except anthropic.BadRequestError as exc:
            # 400 — не сетевая проблема; ретрай не чинит, сразу в фолбэк.
            raise SchemaRejected(str(exc)) from exc
        except RETRYABLE as exc:
            last = exc
            if attempts_left > 0:
                time.sleep(delay * 2**attempt_no)
            continue
        _charge(resp.usage)
        return resp, attempts_left
    assert last is not None
    raise last


def call(
    prompt: str,
    schema: dict,
    schema_version: str,
    document_b64: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    blocks = _build_blocks(prompt, document_b64)
    key = cache_key(MODEL, blocks, schema, schema_version)

    cassette_path = CASSETTE / f"{key}.json"
    if cassette_path.exists():
        return json.loads(cassette_path.read_text())["result"]

    cache_path = CACHE / f"{key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())["result"]

    if os.environ.get("LLM_OFFLINE") == "1":
        raise CassetteMiss(f"нет кассеты для ключа {key}: перезаписать `make cassette-freeze`")

    with _budget_lock:
        if _budget["spent_usd"] >= _budget["ceiling_usd"]:
            raise BudgetExhausted(f"spent {_budget['spent_usd']:.2f} >= {_budget['ceiling_usd']:.2f} USD")

    request_blocks = _with_cache_control(blocks)
    delay = 1.0 + int(key[:4], 16) / 65536  # детерминированный джиттер

    attempts_left = MAX_ATTEMPTS
    resp, attempts_left = _request(request_blocks, schema, max_tokens, delay, attempts_left)

    # stop_reason проверяется до чтения контента: обрезанный/отказанный
    # ответ не должен тихо пройти как валидный результат.
    if resp.stop_reason == "refusal":
        raise SchemaRejected("модель отказалась отвечать (stop_reason=refusal)")
    if resp.stop_reason == "max_tokens":
        if attempts_left <= 0:
            # Бюджет попыток (общий с ретраями транзиентных ошибок) уже
            # исчерпан — тихо возвращать обрезанный JSON нельзя, а второй
            # сетевой вызов сверх MAX_ATTEMPTS запрещён спекой.
            raise SchemaRejected("ответ обрезан по max_tokens: попытки исчерпаны")
        resp, attempts_left = _request(request_blocks, schema, max_tokens * 2, delay, attempts_left)
        if resp.stop_reason == "max_tokens":
            raise SchemaRejected("ответ обрезан по max_tokens дважды подряд")

    tool_blocks = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    if not tool_blocks:
        raise SchemaRejected("модель не вызвала emit")
    result = tool_blocks[0].input
    try:
        jsonschema.validate(result, schema)
    except jsonschema.ValidationError as exc:
        raise SchemaRejected(str(exc)) from exc

    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(stable_json({"result": result}))
    tmp.replace(cache_path)
    return result
