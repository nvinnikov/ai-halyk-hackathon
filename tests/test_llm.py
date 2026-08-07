"""Кэш адресуется содержимым; провал не кэшируется; джиттер — из ключа."""

import json

import anthropic
import httpx
import pytest

import llm

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


class FakeUsage:
    input_tokens = 100
    output_tokens = 10


class FakeBlock:
    type = "tool_use"

    def __init__(self, data):
        self.input = data


class FakeResp:
    usage = FakeUsage()

    def __init__(self, data, stop_reason="tool_use"):
        self.content = [FakeBlock(data)]
        self.stop_reason = stop_reason


def bad_request_error(message="bad request"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError(message, response=resp, body=None)


def rate_limit_error(message="rate limited"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    return anthropic.RateLimitError(message, response=resp, body=None)


def gemini_response(status_code, body=None, headers=None):
    req = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")
    return httpx.Response(status_code, json=body if body is not None else {}, headers=headers, request=req)


def gemini_ok(result, finish_reason="STOP", extra_parts=None, prompt_tokens=10, output_tokens=5):
    parts = [*(extra_parts or []), {"text": json.dumps(result)}]
    return gemini_response(
        200,
        {
            "candidates": [{"finishReason": finish_reason, "content": {"parts": parts}}],
            "usageMetadata": {"promptTokenCount": prompt_tokens, "candidatesTokenCount": output_tokens},
        },
    )


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE", tmp_path / "llm_cache")
    monkeypatch.setattr(llm, "CASSETTE", tmp_path / "cassette")
    monkeypatch.setattr(llm, "_budget", {"spent_usd": 0.0, "ceiling_usd": 10.0})
    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_MIN_INTERVAL_MS", raising=False)
    monkeypatch.setattr(llm, "_gemini_next_allowed", 0.0)


def test_cache_key_depends_on_all_parts():
    k = llm.cache_key("m", [{"t": "p"}], SCHEMA, "v1")
    assert k != llm.cache_key("m", [{"t": "p"}], SCHEMA, "v2")
    assert k != llm.cache_key("m2", [{"t": "p"}], SCHEMA, "v1")
    assert k == llm.cache_key("m", [{"t": "p"}], SCHEMA, "v1")


def test_success_is_cached(monkeypatch):
    calls = []

    def fake_create(**kw):
        calls.append(1)
        return FakeResp({"a": 5})

    monkeypatch.setattr(llm, "_create", fake_create)
    assert llm.call("p", SCHEMA, "v1") == {"a": 5}
    assert llm.call("p", SCHEMA, "v1") == {"a": 5}
    assert len(calls) == 1


def test_schema_failure_not_cached_and_not_retried(monkeypatch):
    calls = []

    def fake_create(**kw):
        calls.append(1)
        return FakeResp({"wrong": True})

    monkeypatch.setattr(llm, "_create", fake_create)
    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")
    assert len(calls) == 1
    assert not list(llm.CACHE.glob("*.json"))


def test_retry_on_rate_limit_then_success(monkeypatch):
    calls = []

    def fake_create(**kw):
        calls.append(1)
        if len(calls) < 3:
            raise anthropic.APIConnectionError(request=None)
        return FakeResp({"a": 1})

    monkeypatch.setattr(llm, "_create", fake_create)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.call("p", SCHEMA, "v1") == {"a": 1}
    assert len(calls) == 3


def test_retries_exhausted_raises(monkeypatch):
    def fake_create(**kw):
        raise anthropic.APIConnectionError(request=None)

    monkeypatch.setattr(llm, "_create", fake_create)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(anthropic.APIConnectionError):
        llm.call("p", SCHEMA, "v1")


def test_budget_ceiling(monkeypatch):
    monkeypatch.setattr(llm, "_budget", {"spent_usd": 10.0, "ceiling_usd": 10.0})
    with pytest.raises(llm.BudgetExhausted):
        llm.call("p", SCHEMA, "v1")


# --- Правки по ревью ---


def test_bad_request_is_schema_rejected_no_retry_no_cache(monkeypatch):
    calls = []

    def fake_create(**kw):
        calls.append(1)
        raise bad_request_error()

    monkeypatch.setattr(llm, "_create", fake_create)
    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")
    assert len(calls) == 1
    assert not list(llm.CACHE.glob("*.json"))


def test_max_tokens_retries_once_with_doubled_limit_then_succeeds(monkeypatch):
    calls = []

    def fake_create(**kw):
        calls.append(kw["max_tokens"])
        if len(calls) == 1:
            return FakeResp({}, stop_reason="max_tokens")
        return FakeResp({"a": 1}, stop_reason="tool_use")

    monkeypatch.setattr(llm, "_create", fake_create)
    assert llm.call("p", SCHEMA, "v1", max_tokens=1000) == {"a": 1}
    assert calls == [1000, 2000]


def test_max_tokens_twice_raises_schema_rejected(monkeypatch):
    calls = []

    def fake_create(**kw):
        calls.append(kw["max_tokens"])
        return FakeResp({}, stop_reason="max_tokens")

    monkeypatch.setattr(llm, "_create", fake_create)
    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1", max_tokens=1000)
    assert calls == [1000, 2000]
    assert not list(llm.CACHE.glob("*.json"))


def test_refusal_is_schema_rejected_without_retry(monkeypatch):
    calls = []

    def fake_create(**kw):
        calls.append(1)
        return FakeResp({}, stop_reason="refusal")

    monkeypatch.setattr(llm, "_create", fake_create)
    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")
    assert len(calls) == 1
    assert not list(llm.CACHE.glob("*.json"))


def test_cassette_hit_takes_priority_over_network(monkeypatch):
    def fake_create(**kw):
        raise AssertionError("сеть не должна вызываться при попадании в кассету")

    monkeypatch.setattr(llm, "_create", fake_create)

    key = llm.cache_key(llm.MODEL, [{"type": "text", "text": "p"}], SCHEMA, "v1")
    llm.CASSETTE.mkdir(parents=True, exist_ok=True)
    (llm.CASSETTE / f"{key}.json").write_text(json.dumps({"result": {"a": 42}}))

    assert llm.call("p", SCHEMA, "v1") == {"a": 42}


def test_offline_without_cassette_raises_cassette_miss(monkeypatch):
    def fake_create(**kw):
        raise AssertionError("сеть не должна вызываться в офлайн-режиме")

    monkeypatch.setattr(llm, "_create", fake_create)
    monkeypatch.setenv("LLM_OFFLINE", "1")

    with pytest.raises(llm.CassetteMiss):
        llm.call("p", SCHEMA, "v1")


def test_retry_and_max_tokens_retry_share_one_attempts_budget(monkeypatch):
    """RateLimit-ретрай и max_tokens-повтор списывают попытки из общего
    счётчика MAX_ATTEMPTS=4, а не заводят каждый свой цикл до 4."""
    calls = []

    def fake_create(**kw):
        calls.append(kw["max_tokens"])
        if len(calls) == 1:
            raise rate_limit_error()
        if len(calls) == 2:
            return FakeResp({}, stop_reason="max_tokens")
        return FakeResp({"a": 9}, stop_reason="tool_use")

    monkeypatch.setattr(llm, "_create", fake_create)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    assert llm.call("p", SCHEMA, "v1", max_tokens=1000) == {"a": 9}
    assert calls == [1000, 1000, 2000]
    assert len(calls) <= llm.MAX_ATTEMPTS


def test_attempts_budget_exhausted_before_max_tokens_retry_raises_schema_rejected(monkeypatch):
    """Если ретраи на RateLimit уже съели весь бюджет попыток и последний
    оставшийся ответ обрезан по max_tokens — второй сетевой вызов сверх
    MAX_ATTEMPTS не делается, сразу SchemaRejected."""
    calls = []

    def fake_create(**kw):
        calls.append(kw["max_tokens"])
        if len(calls) < llm.MAX_ATTEMPTS:
            raise rate_limit_error()
        return FakeResp({}, stop_reason="max_tokens")

    monkeypatch.setattr(llm, "_create", fake_create)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1", max_tokens=1000)
    assert len(calls) == llm.MAX_ATTEMPTS
    assert not list(llm.CACHE.glob("*.json"))


def test_offline_with_cache_hit_does_not_raise(monkeypatch):
    def fake_create(**kw):
        raise AssertionError("сеть не должна вызываться при попадании в work-кэш")

    monkeypatch.setattr(llm, "_create", fake_create)

    key = llm.cache_key(llm.MODEL, [{"type": "text", "text": "p"}], SCHEMA, "v1")
    llm.CACHE.mkdir(parents=True, exist_ok=True)
    (llm.CACHE / f"{key}.json").write_text(json.dumps({"result": {"a": 7}}))

    monkeypatch.setenv("LLM_OFFLINE", "1")
    assert llm.call("p", SCHEMA, "v1") == {"a": 7}


# --- Gemini-бэкенд (переключатель LLM_PROVIDER) ---


def test_provider_defaults_to_anthropic():
    assert llm._provider() == "anthropic"


def test_provider_reads_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm._provider() == "gemini"


def test_call_dispatches_to_gemini_when_selected(monkeypatch):
    def fail_anthropic(**kw):
        raise AssertionError("anthropic не должен вызываться при LLM_PROVIDER=gemini")

    calls = []

    def fake_gemini_create(model, body):
        calls.append(model)
        return gemini_ok({"a": 1})

    monkeypatch.setattr(llm, "_create", fail_anthropic)
    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    assert llm.call("p", SCHEMA, "v1") == {"a": 1}
    assert calls == [llm.GEMINI_MODEL]


def test_gemini_create_builds_url_and_api_key_header(monkeypatch):
    calls = []

    class FakeHttpxClient:
        def post(self, url, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            return gemini_ok({"a": 1})

    monkeypatch.setattr(llm, "_gemini_client", FakeHttpxClient())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")

    llm._gemini_create(llm.GEMINI_MODEL, {"contents": []})

    assert calls[0]["url"] == llm.GEMINI_URL.format(model=llm.GEMINI_MODEL)
    assert calls[0]["headers"]["X-goog-api-key"] == "test-key-123"


def test_gemini_body_has_response_mime_type_and_inline_pdf(monkeypatch):
    captured = {}

    def fake_gemini_create(model, body):
        captured["model"] = model
        captured["body"] = body
        return gemini_ok({"a": 1})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    llm.call("prompt text", SCHEMA, "v1", document_b64="QkFTRTY0")

    assert captured["model"] == llm.GEMINI_MODEL
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    parts = captured["body"]["contents"][0]["parts"]
    assert {"text": "prompt text"} in parts
    inline = next(p["inline_data"] for p in parts if "inline_data" in p)
    assert inline == {"mime_type": "application/pdf", "data": "QkFTRTY0"}


def test_gemini_unwraps_emit_key_quirk(monkeypatch):
    """Живым вызовом (test_gemini_live_smoke) воспроизведено: промпты,
    написанные под anthropic tool-calling («верни результат через emit»),
    без настоящего tool у gemini заставляют модель буквально завернуть ответ
    в {"emit": {...}}. call() должен развернуть это прозрачно для потребителя."""

    def fake_gemini_create(model, body):
        return gemini_ok({"emit": {"a": 4}})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    assert llm.call("верни результат через emit", SCHEMA, "v1") == {"a": 4}


def test_gemini_does_not_unwrap_multi_key_dict_named_emit(monkeypatch):
    """Разворачивается только объект вида {"emit": {...}} без соседей —
    иначе легитимный ответ с полем emit молча потерял бы остальные ключи."""

    def fake_gemini_create(model, body):
        return gemini_ok({"emit": {"a": 4}, "other": 1})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")


def test_gemini_validates_before_unwrapping_emit(monkeypatch):
    """Validate-first (ревью, Important 1): если ответ уже проходит схему КАК
    ЕСТЬ — в т.ч. случайно совпав по форме с emit-обёрткой, потому что у
    схемы легитимно есть поле "emit" — разворот не применяется вовсе."""
    schema_with_emit_field = {
        "type": "object",
        "properties": {"emit": {"type": "object"}},
        "required": ["emit"],
    }

    def fake_gemini_create(model, body):
        return gemini_ok({"emit": {"a": 1}})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    assert llm.call("p", schema_with_emit_field, "v1") == {"emit": {"a": 1}}


def test_gemini_unwrap_failure_raises_original_error_not_wrapper_error(monkeypatch):
    """Невалидный ответ без обёртки → SchemaRejected с ОРИГИНАЛЬНОЙ ошибкой
    прямой валидации, а не с ошибкой попытки разворота."""

    def fake_gemini_create(model, body):
        return gemini_ok({"wrong": True})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(llm.SchemaRejected) as exc_info:
        llm.call("p", SCHEMA, "v1")
    assert "'a' is a required property" in str(exc_info.value)


def test_gemini_anthropic_provider_never_unwraps_emit(monkeypatch):
    """Разворот — исключительно gemini-квирк; на anthropic-пути (даже если
    бы туда как-то попал такой ответ) поведение не должно меняться."""

    def fake_create(**kw):
        return FakeResp({"emit": {"a": 4}})

    monkeypatch.setattr(llm, "_create", fake_create)

    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")


def test_gemini_response_skips_thought_parts(monkeypatch):
    def fake_gemini_create(model, body):
        return gemini_ok({"a": 7}, extra_parts=[{"text": "рассуждение...", "thought": True}])

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    assert llm.call("p", SCHEMA, "v1") == {"a": 7}


def test_gemini_retries_429_then_succeeds(monkeypatch):
    calls = []

    def fake_gemini_create(model, body):
        calls.append(1)
        if len(calls) < 3:
            return gemini_response(429, {"error": {"message": "rate limited"}})
        return gemini_ok({"a": 3})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    assert llm.call("p", SCHEMA, "v1") == {"a": 3}
    assert len(calls) == 3


def test_gemini_retries_exhausted_raises(monkeypatch):
    def fake_gemini_create(model, body):
        return gemini_response(503, {})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(llm.GeminiTransientError):
        llm.call("p", SCHEMA, "v1")


def test_gemini_retry_delay_prefers_retry_after_header():
    resp = gemini_response(429, {}, headers={"Retry-After": "7"})
    assert llm._gemini_retry_delay(resp, fallback=99.0) == 7.0


def test_gemini_retry_delay_reads_retry_delay_from_body():
    resp = gemini_response(429, {"error": {"details": [{"retryDelay": "3s"}]}})
    assert llm._gemini_retry_delay(resp, fallback=99.0) == 3.0


def test_gemini_retry_delay_falls_back_when_absent():
    resp = gemini_response(429, {})
    assert llm._gemini_retry_delay(resp, fallback=5.5) == 5.5


def test_safe_error_text_redacts_api_key_and_truncates(monkeypatch):
    """Minor (ревью): если Google эхом вернёт заголовки запроса (в т.ч.
    X-goog-api-key) в теле ошибки, ключ не должен попасть в сообщение
    исключения — сообщение может улететь в лог/трейс."""
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-abc")
    body = {"error": {"message": "X-goog-api-key: secret-key-abc" + "x" * 600}}
    resp = gemini_response(403, body)

    text = llm._safe_error_text(resp, limit=500)

    assert "secret-key-abc" not in text
    assert len(text) <= 500


def test_safe_error_text_redacts_key_straddling_truncation_boundary(monkeypatch):
    """Редактируем ДО обрезки, не после: если бы порядок был обратный, ключ,
    оказавшийся на границе limit, обрезался бы посередине — фрагмент ключа
    остался бы в сообщении, потому что усечённый текст уже не совпадает с
    ключом целиком для .replace()."""
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-abc")
    prefix = "x" * 495  # ключ (14 симв.) начинается за 5 символов до limit=500
    raw = prefix + "secret-key-abc" + "y" * 50
    req = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")
    resp = httpx.Response(403, text=raw, request=req)

    text = llm._safe_error_text(resp, limit=500)

    # даже фрагмент ключа не должен просочиться (маркер [REDACTED] сам может
    # быть обрезан лимитом — это ожидаемо, важно отсутствие секрета).
    assert "secre" not in text


def test_gemini_max_tokens_retries_once_with_doubled_limit(monkeypatch):
    calls = []

    def fake_gemini_create(model, body):
        calls.append(body["generationConfig"]["maxOutputTokens"])
        if len(calls) == 1:
            return gemini_response(
                200,
                {
                    "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}],
                    "usageMetadata": {},
                },
            )
        return gemini_ok({"a": 1})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    assert llm.call("p", SCHEMA, "v1", max_tokens=1000) == {"a": 1}
    assert calls == [1000, 2000]


def test_gemini_safety_finish_is_schema_rejected(monkeypatch):
    def fake_gemini_create(model, body):
        return gemini_response(
            200,
            {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}], "usageMetadata": {}},
        )

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")


def test_gemini_invalid_json_is_schema_rejected_no_retry_no_cache(monkeypatch):
    calls = []

    def fake_gemini_create(model, body):
        calls.append(1)
        return gemini_response(
            200,
            {
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not json"}]}}],
                "usageMetadata": {},
            },
        )

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")
    assert len(calls) == 1
    assert not list(llm.CACHE.glob("*.json"))


def test_gemini_schema_mismatch_is_schema_rejected_no_retry_no_cache(monkeypatch):
    calls = []

    def fake_gemini_create(model, body):
        calls.append(1)
        return gemini_ok({"wrong": True})

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(llm.SchemaRejected):
        llm.call("p", SCHEMA, "v1")
    assert len(calls) == 1
    assert not list(llm.CACHE.glob("*.json"))


def test_cache_key_differs_by_provider_model():
    k_anthropic = llm.cache_key(llm.MODEL, [{"type": "text", "text": "p"}], SCHEMA, "v1")
    k_gemini = llm.cache_key(llm.GEMINI_MODEL, [{"type": "text", "text": "p"}], SCHEMA, "v1")
    assert k_anthropic != k_gemini


def test_call_uses_separate_cache_entries_per_provider(monkeypatch):
    def fake_create(**kw):
        return FakeResp({"a": 1})

    def fake_gemini_create(model, body):
        return gemini_ok({"a": 2})

    monkeypatch.setattr(llm, "_create", fake_create)
    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)

    assert llm.call("p", SCHEMA, "v1") == {"a": 1}
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm.call("p", SCHEMA, "v1") == {"a": 2}

    assert len(list(llm.CACHE.glob("*.json"))) == 2


def test_gemini_charges_budget_including_thoughts_tokens(monkeypatch):
    def fake_gemini_create(model, body):
        return gemini_response(
            200,
            {
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"a": 1}'}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 10,
                    "thoughtsTokenCount": 20,
                },
            },
        )

    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    llm.call("p", SCHEMA, "v1")

    expected = 100 * llm.GEMINI_PRICE_IN + (10 + 20) * llm.GEMINI_PRICE_OUT
    assert llm._budget["spent_usd"] == pytest.approx(expected)


# --- Rate-limiter gemini-ветки (ревью, Important 2) ---


def _fake_clock(monkeypatch):
    """Управляемые monotonic()/sleep() без реального ожидания: sleep(s)
    просто продвигает часы на s, monotonic() читает их текущее значение."""
    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    def fake_sleep(s):
        clock["t"] += s

    monkeypatch.setattr(llm.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(llm.time, "sleep", fake_sleep)
    return clock


def test_gemini_throttle_disabled_by_default(monkeypatch):
    clock = _fake_clock(monkeypatch)
    llm._gemini_throttle()
    llm._gemini_throttle()
    assert clock["t"] == 0.0  # ни одного sleep — интервал выключен (дефолт 0)


def test_gemini_throttle_enforces_min_interval(monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL_MS", "500")
    clock = _fake_clock(monkeypatch)

    llm._gemini_throttle()
    assert clock["t"] == 0.0  # первый вызов — ждать некого

    clock["t"] += 0.1  # прошло 100мс — меньше интервала в 500мс
    llm._gemini_throttle()
    assert clock["t"] == pytest.approx(0.5)  # sleep добрал оставшиеся 400мс


def test_gemini_throttle_no_wait_if_interval_already_elapsed(monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL_MS", "500")
    clock = _fake_clock(monkeypatch)

    llm._gemini_throttle()
    clock["t"] += 1.0  # прошло больше интервала — ждать не нужно
    llm._gemini_throttle()
    assert clock["t"] == pytest.approx(1.0)  # часы не сдвинуты sleep'ом


def test_gemini_request_throttles_before_each_network_attempt(monkeypatch):
    calls = []

    def fake_throttle():
        calls.append("throttle")

    def fake_gemini_create(model, body):
        calls.append("create")
        return gemini_ok({"a": 1})

    monkeypatch.setattr(llm, "_gemini_throttle", fake_throttle)
    monkeypatch.setattr(llm, "_gemini_create", fake_gemini_create)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    llm.call("p", SCHEMA, "v1")
    assert calls == ["throttle", "create"]


def test_anthropic_path_never_throttles(monkeypatch):
    """anthropic-ветку не трогаем (условие фикса) — _gemini_throttle не вызывается."""
    calls = []
    monkeypatch.setattr(llm, "_gemini_throttle", lambda: calls.append(1))
    monkeypatch.setattr(llm, "_create", lambda **kw: FakeResp({"a": 1}))

    assert llm.call("p", SCHEMA, "v1") == {"a": 1}
    assert calls == []


@pytest.mark.llm
def test_gemini_live_smoke():
    """Единственный живой вызов Gemini (не входит в make check — addopts
    исключает marker llm). Требует GEMINI_API_KEY в окружении."""
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    import os

    old = os.environ.get("LLM_PROVIDER")
    os.environ["LLM_PROVIDER"] = "gemini"
    try:
        result = llm.call(
            "Сколько будет 2+2? Верни результат через emit с целочисленным полем answer.",
            schema,
            "gemini-smoke-1",
        )
    finally:
        if old is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = old
    print("GEMINI SMOKE RESULT:", result)
    assert result["answer"] == 4
