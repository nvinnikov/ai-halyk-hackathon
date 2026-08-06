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


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE", tmp_path / "llm_cache")
    monkeypatch.setattr(llm, "CASSETTE", tmp_path / "cassette")
    monkeypatch.setattr(llm, "_budget", {"spent_usd": 0.0, "ceiling_usd": 10.0})
    monkeypatch.delenv("LLM_OFFLINE", raising=False)


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


def test_offline_with_cache_hit_does_not_raise(monkeypatch):
    def fake_create(**kw):
        raise AssertionError("сеть не должна вызываться при попадании в work-кэш")

    monkeypatch.setattr(llm, "_create", fake_create)

    key = llm.cache_key(llm.MODEL, [{"type": "text", "text": "p"}], SCHEMA, "v1")
    llm.CACHE.mkdir(parents=True, exist_ok=True)
    (llm.CACHE / f"{key}.json").write_text(json.dumps({"result": {"a": 7}}))

    monkeypatch.setenv("LLM_OFFLINE", "1")
    assert llm.call("p", SCHEMA, "v1") == {"a": 7}
