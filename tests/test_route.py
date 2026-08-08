"""Кандидаты — только целевые счета; сшивка при нескольких кандидатах запрещена.

Фоновый документ (только нецелевые счета леджера) — штатный карантин без
алярма и без LLM-вызовов: на приватном наборе таких большинство.
"""

from pathlib import Path

import pytest

import route

TARGETS = ["ACC-1111", "ACC-2222"]
ALL_ACCOUNTS = ["ACC-1111", "ACC-2222", "ACC-9001"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    state = {"text": "", "llm": []}

    monkeypatch.setattr(route, "full_text", lambda wd, p: state["text"])
    monkeypatch.setattr(route, "first_page_text", lambda wd, p: state["text"])
    monkeypatch.setattr(route, "doc_hash", lambda p: "cafe00000000")

    def fake_call(prompt, schema, schema_version, **kw):
        state["llm"].append(prompt)
        if schema is route.WHOSE_SCHEMA:
            return {"account_id": "ACC-1111", "quote": "договор с заёмщиком ACC-1111"}
        return {"doc_type": "agreement", "date": "2025-03-01", "edition": "final"}

    monkeypatch.setattr(route.llm, "call", fake_call)
    return state, tmp_path


def test_single_candidate_binds(fake):
    state, wd = fake
    state["text"] = "Договор займа, счёт заёмщика ACC-1111, фоновый счёт ACC-9001"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] == "ACC-1111"
    assert art["quarantined"] is False
    assert art["alarms"] == []
    assert art["doc_type"] == "agreement" and art["edition"] == "final"
    assert art["mentions_nontarget"] == ["ACC-9001"]


def test_background_document_quarantined_without_alarm(fake):
    # Документ про чужой счёт отбрасывается штатно: без алярма и без LLM.
    state, wd = fake
    state["text"] = "упомянут только фоновый ACC-9001 и несуществующий ACC-0000"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert art["quarantine_reason"] == "background_document"
    assert art["alarms"] == []
    assert art["doc_type"] == "unrouted"
    assert state["llm"] == []  # ни META, ни WHOSE не вызывались


def test_no_accounts_at_all_is_alarm(fake):
    state, wd = fake
    state["text"] = "текст вовсе без номеров счетов"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert any(a["kind"] == "routing_quarantine" for a in art["alarms"])
    assert art["doc_type"] == "unrouted"
    assert state["llm"] == []


def test_multiple_candidates_go_to_llm_with_alarm(fake):
    state, wd = fake
    # Цитата WHOSE обязана находиться в тексте, иначе кандидат не подтверждён.
    state["text"] = "договор с заёмщиком ACC-1111, переводы между ACC-1111 и ACC-2222"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] == "ACC-1111"
    assert art["routing_quote"] == "договор с заёмщиком ACC-1111"
    assert any(a["kind"] == "ambiguous_routing" for a in art["alarms"])


def test_llm_answer_outside_candidates_is_quarantine(fake, monkeypatch):
    state, wd = fake
    state["text"] = "переводы между ACC-1111 и ACC-2222"

    def bad_call(prompt, schema, schema_version, **kw):
        if schema is route.WHOSE_SCHEMA:
            return {"account_id": "ACC-9999", "quote": "..."}
        return {"doc_type": "other", "date": "", "edition": "unmarked"}

    monkeypatch.setattr(route.llm, "call", bad_call)
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True


def test_unverified_whose_quote_quarantines(fake):
    state, wd = fake
    state["text"] = "переводы между ACC-1111 и ACC-2222"  # цитаты WHOSE в тексте нет
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert any(a["kind"] == "quote_unverified" for a in art["alarms"])


def test_mention_search_respects_boundaries(fake):
    state, wd = fake
    # ACC-111 не должен находиться внутри ACC-1111 подстрочным поиском.
    state["text"] = "счёт заёмщика ACC-1111"
    art = route.route_doc(wd, Path("x.pdf"), ["ACC-111", "ACC-1111"], ["ACC-111", "ACC-1111"])
    assert art["mentions"] == ["ACC-1111"]
    assert art["account_id"] == "ACC-1111"


def test_non_client_doc_type_not_bound(fake, monkeypatch):
    """Методичка с целевым счётом внутри — не документ клиента: тип other
    не привязывается даже при единственном кандидате."""
    state, wd = fake

    def call_other(prompt, schema, schema_version, **kw):
        state["llm"].append(prompt)
        return {"doc_type": "other", "date": "", "edition": "unmarked"}

    monkeypatch.setattr(route.llm, "call", call_other)
    state["text"] = "методичка комплаенса, упоминает ACC-1111"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art["account_id"] is None and art["quarantined"] is True
    assert art["quarantine_reason"] == "non_client_doc_type"
    assert not any(a["kind"] == "routing_quarantine" for a in art["alarms"])


def test_meta_failure_not_cached(fake, monkeypatch):
    """Провал META (SchemaRejected → карантин non_client_doc_type) не
    оставляет route-артефакта: перезапуск после устранения причины
    перемаршрутизирует (ревью PR #9, 23-я волна)."""
    import llm as llm_mod

    state, wd = fake

    def meta_fails(prompt, schema, schema_version, **kw):
        raise llm_mod.SchemaRejected("schema mismatch")

    monkeypatch.setattr(route.llm, "call", meta_fails)
    state["text"] = "Договор займа, счёт заёмщика ACC-1111"
    art = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert any(a["kind"] == "meta_extraction_failed" for a in art["alarms"])
    assert not (wd / "route" / "cafe00000000.json").exists()

    # Причина устранена — маршрутизация проходит и кэшируется.
    def meta_ok(prompt, schema, schema_version, **kw):
        return {"doc_type": "agreement", "date": "2025-03-01", "edition": "final"}

    monkeypatch.setattr(route.llm, "call", meta_ok)
    art2 = route.route_doc(wd, Path("x.pdf"), TARGETS, ALL_ACCOUNTS)
    assert art2["account_id"] == "ACC-1111"
    assert (wd / "route" / "cafe00000000.json").exists()
