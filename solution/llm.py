"""Клиент LLM: content-addressed кэш, ретраи, потолок бюджета.

Провайдер выбирается через env `LLM_PROVIDER` (`anthropic` | `gemini`,
дефолт — `anthropic`, текущее поведение). Anthropic-ветка — основной путь,
gemini-ветка — переключатель на случай исчерпанного баланса Anthropic;
интерфейс `call()` для потребителей (route/facts_extract/specs_extract/
categorize_llm/vision) не меняется вне зависимости от провайдера.

Ключ кэша = sha256(model + prompt + json_schema + schema_version) — раздел 3
спеки. Модель — часть ключа, поэтому у gemini и anthropic кэш не пересекается.
Кэш общий между наборами, никогда не инвалидируется по времени; в кэш
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
import httpx
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

# Пин конкретного id, не алиас (gemini-flash-latest дрейфует, а кэш ключуется
# строкой модели — дрейф алиаса тихо разъехался бы с уже накопленным кэшем).
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Точный прайс 3.6-flash не опубликован на момент написания; ставим верхнюю
# оценку по соседним Flash-моделям, округлённую вверх — бюджетный потолок
# должен скорее сработать раньше, чем пропустить реальный перерасход.
GEMINI_PRICE_IN, GEMINI_PRICE_OUT = 0.5e-6, 3e-6
# 429 — норма для бесплатного тарифа Gemini, ретраим наравне с 5xx.
RETRYABLE_GEMINI_STATUS = {429, 500, 502, 503, 504}

_budget = {"spent_usd": 0.0, "ceiling_usd": float(os.environ.get("LLM_BUDGET_USD", "50"))}
_budget_lock = threading.Lock()
_client: anthropic.Anthropic | None = None
_gemini_client: httpx.Client | None = None
_gemini_rate_lock = threading.Lock()
# time.monotonic() следующего разрешённого запроса — см. _gemini_throttle().
_gemini_next_allowed = 0.0


def _provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic")


class BudgetExhausted(Exception):
    """Потолок стоимости прогона: дальше — фолбэки из уже посчитанного."""


class SchemaRejected(Exception):
    """Ответ модели не прошёл схему: не сетевая проблема, ретрай не чинит."""


class CassetteMiss(Exception):
    """LLM_OFFLINE=1, а ни кассета, ни work-кэш не содержат этот ключ."""


class GeminiTransientError(Exception):
    """429/5xx у gemini после исчерпания ретраев (аналог RETRYABLE у anthropic)."""


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
            # Поле thinking не передаётся: haiku-4-5 не принимает adaptive
            # (400 «not supported on this model»), а модели 4.6+ при пропуске
            # поля включают adaptive сами. Задача модели здесь — чтение и
            # переписывание текста, thinking для неё не нужен.
            resp = _create(
                model=MODEL,
                max_tokens=max_tokens,
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


def _gemini_throttle() -> None:
    """Глобальный (на весь процесс) минимальный интервал между запросами к
    Gemini — GEMINI_MIN_INTERVAL_MS (мс), дефолт 0 = выключено, поведение как
    раньше.

    Зачем: фаза роутинга документов (build_dossiers) бьёт по gemini несколькими
    потоками разом (ThreadPoolExecutor SOLVE_WORKERS=4, до 2 вызовов на
    документ) без координации между собой; на бесплатном тарифе (~10-15 RPM)
    это может синхронно исчерпать ретраи (MAX_ATTEMPTS=4 — независимый бюджет
    у каждого потока) — документы уйдут в карантин routing_failed без
    диагностики. Включать явно перед боевым gemini-прогоном.
    """
    interval = float(os.environ.get("GEMINI_MIN_INTERVAL_MS", "0")) / 1000.0
    if interval <= 0:
        return
    global _gemini_next_allowed
    with _gemini_rate_lock:
        now = time.monotonic()
        wait = _gemini_next_allowed - now
        if wait > 0:
            time.sleep(wait)
            now = _gemini_next_allowed
        _gemini_next_allowed = now + interval


def _safe_error_text(resp: httpx.Response, limit: int = 500) -> str:
    """Текст ошибки для сообщения исключения: без значения GEMINI_API_KEY (на
    случай, если Google вернёт заголовки запроса эхом) и обрезан.

    Редактируем ДО обрезки: если бы обрезали сначала, ключ, оказавшийся на
    границе limit, мог бы попасть в сообщение частично — редактирование по
    точному совпадению его уже не поймало бы.
    """
    text = resp.text
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return text[:limit]


def _gemini_create(model: str, body: dict) -> httpx.Response:
    """Единственная точка HTTP-обращения к Gemini — подменяется в тестах."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = httpx.Client(timeout=120.0)
    url = GEMINI_URL.format(model=model)
    headers = {"X-goog-api-key": os.environ.get("GEMINI_API_KEY", "")}
    return _gemini_client.post(url, json=body, headers=headers)


def _gemini_contents(blocks: list) -> list:
    """Blocks в формате anthropic (text/document) → gemini contents.parts.

    Тот же blocks, что уходит в cache_key: формат промпта провайдер-нейтрален,
    в gemini-представление переводится только здесь, на границе HTTP-запроса.
    """
    parts = []
    for b in blocks:
        if b["type"] == "text":
            parts.append({"text": b["text"]})
        elif b["type"] == "document":
            parts.append(
                {
                    "inline_data": {
                        "mime_type": b["source"]["media_type"],
                        "data": b["source"]["data"],
                    }
                }
            )
    return [{"role": "user", "parts": parts}]


def _gemini_retry_delay(resp: httpx.Response, fallback: float) -> float:
    """Retry-After заголовок или retryDelay из тела ошибки, иначе наш бэкоф."""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        details = resp.json().get("error", {}).get("details", [])
    except ValueError:
        details = []
    for d in details:
        rd = d.get("retryDelay")
        if isinstance(rd, str) and rd.endswith("s"):
            try:
                return float(rd[:-1])
            except ValueError:
                continue
    return fallback


def _charge_gemini(usage: dict) -> None:
    # thoughtsTokenCount биллится как output — thinking у 3.6-flash не
    # отключается флагом, только не конфигурируется явно (см. модуль-докстринг).
    input_tokens = usage.get("promptTokenCount", 0)
    output_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    cost = input_tokens * GEMINI_PRICE_IN + output_tokens * GEMINI_PRICE_OUT
    with _budget_lock:
        _budget["spent_usd"] += cost


class _GeminiBlock:
    """Мимикрирует под anthropic tool_use-блок, чтобы call() читал content
    одинаково для обоих провайдеров."""

    type = "tool_use"

    def __init__(self, data):
        self.input = data


class _GeminiResp:
    """Нормализованный ответ gemini под тот же интерфейс, что call() ожидает
    от anthropic Message: .stop_reason и .content с tool_use-блоками."""

    def __init__(self, stop_reason: str, result: dict | None):
        self.stop_reason = stop_reason
        self.content = [_GeminiBlock(result)] if result is not None else []


_GEMINI_FINISH_MAX_TOKENS = {"MAX_TOKENS"}
# Любой не-STOP финиш вне MAX_TOKENS трактуем как отказ (SAFETY, RECITATION,
# PROHIBITED_CONTENT, SPII, BLOCKLIST, OTHER и т.п. — список у gemini длиннее
# наших случаев use, не перечисляем поимённо).
_GEMINI_FINISH_OK = {"STOP", None}


def _request_gemini(blocks: list, max_tokens: int, delay: float, attempts_left: int):
    """Аналог _request для gemini: тот же общий бюджет попыток MAX_ATTEMPTS,
    тот же экспоненциальный бэкоф на транзиентных ошибках (429/5xx)."""
    body = {
        "contents": _gemini_contents(blocks),
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": max_tokens,
        },
    }
    last: Exception | None = None
    while attempts_left > 0:
        attempt_no = MAX_ATTEMPTS - attempts_left
        attempts_left -= 1
        _gemini_throttle()
        resp = _gemini_create(GEMINI_MODEL, body)
        if resp.status_code in RETRYABLE_GEMINI_STATUS:
            last = GeminiTransientError(f"gemini {resp.status_code}: {_safe_error_text(resp)}")
            if attempts_left > 0:
                time.sleep(_gemini_retry_delay(resp, delay * 2**attempt_no))
            continue
        if resp.status_code >= 400:
            # Не транзиентная ошибка (400/401/403/404 и т.п.) — ретрай не чинит.
            raise SchemaRejected(f"gemini {resp.status_code}: {_safe_error_text(resp)}")

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            # Заблокировано на входе (promptFeedback), кандидатов нет вовсе.
            raise SchemaRejected(f"gemini не вернул candidates: {data.get('promptFeedback')}")
        cand = candidates[0]
        finish = cand.get("finishReason")
        _charge_gemini(data.get("usageMetadata", {}))

        if finish in _GEMINI_FINISH_MAX_TOKENS:
            return _GeminiResp("max_tokens", None), attempts_left
        if finish not in _GEMINI_FINISH_OK:
            return _GeminiResp("refusal", None), attempts_left

        parts = cand.get("content", {}).get("parts", [])
        # thought-парты помечены полем thought=true (см. модуль-докстринг) —
        # финальный ответ собираем только из остальных text-частей.
        text_parts = [p["text"] for p in parts if p.get("text") and not p.get("thought")]
        if not text_parts:
            raise SchemaRejected("gemini не вернул текстовую часть ответа")
        try:
            result = json.loads("".join(text_parts))
        except json.JSONDecodeError as exc:
            raise SchemaRejected(f"gemini вернул невалидный JSON: {exc}") from exc
        # «emit»-обёртку (см. call()) здесь не разворачиваем: решение "похоже
        # на обёртку или легитимный ответ" зависит от схемы, а схема сюда не
        # передаётся — разворот только после провала прямой валидации, в call().
        return _GeminiResp("tool_use", result), attempts_left
    assert last is not None
    raise last


def call(
    prompt: str,
    schema: dict,
    schema_version: str,
    document_b64: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    provider = _provider()
    blocks = _build_blocks(prompt, document_b64)
    model = GEMINI_MODEL if provider == "gemini" else MODEL
    key = cache_key(model, blocks, schema, schema_version)

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

    delay = 1.0 + int(key[:4], 16) / 65536  # детерминированный джиттер

    # send(mt, attempts_left) — общая точка входа в сеть для обоих провайдеров;
    # дальше call() читает resp.stop_reason/resp.content одинаково для обоих.
    if provider == "gemini":

        def send(mt, attempts_left):
            return _request_gemini(blocks, mt, delay, attempts_left)
    else:
        request_blocks = _with_cache_control(blocks)

        def send(mt, attempts_left):
            return _request(request_blocks, schema, mt, delay, attempts_left)

    attempts_left = MAX_ATTEMPTS
    resp, attempts_left = send(max_tokens, attempts_left)

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
        resp, attempts_left = send(max_tokens * 2, attempts_left)
        if resp.stop_reason == "max_tokens":
            raise SchemaRejected("ответ обрезан по max_tokens дважды подряд")

    tool_blocks = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    if not tool_blocks:
        raise SchemaRejected("модель не вызвала emit")
    result = tool_blocks[0].input
    try:
        jsonschema.validate(result, schema)
    except jsonschema.ValidationError as exc:
        # «emit»-обёртка: часть промптов пишет «верни результат через emit» —
        # артефакт anthropic-конвенции принудительного tool-calling (см.
        # tool_choice={"name": "emit"} в _request() выше). Промпты не трогаем
        # (условие задачи), а у gemini без настоящего tool это читается
        # буквально — модель заворачивает ответ в {"emit": {...}}. Живым
        # smoke-вызовом (test_gemini_live_smoke) воспроизведено: {"answer": 4}
        # пришёл как {"emit": {"answer": 4}}.
        # Validate-first: разворачиваем ТОЛЬКО когда прямая валидация уже
        # провалилась, и только если развёрнутое проходит схему — иначе
        # наружу уходит оригинальная ошибка, а не ошибка по обёртке.
        unwrapped = result["emit"] if isinstance(result, dict) and list(result.keys()) == ["emit"] else None
        if provider == "gemini" and isinstance(unwrapped, dict):
            try:
                jsonschema.validate(unwrapped, schema)
            except jsonschema.ValidationError:
                raise SchemaRejected(str(exc)) from exc
            result = unwrapped
        else:
            raise SchemaRejected(str(exc)) from exc

    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(stable_json({"result": result}))
    tmp.replace(cache_path)
    return result
