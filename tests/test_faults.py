"""Оборванная сеть и 429 не имеют права оставить невалидный submission.

work/<hash> для публичного архива в этом репозитории уже полностью
прогрет (route/dossier/facts/specs + llm_cache from прошлых прогонов) —
запуск solve.main против него молча прочитает готовые артефакты и не
тронет сеть вовсе, тесты ниже ничего бы не проверили. isolated_workdir
уводит util.WORK/llm.CACHE/llm.CASSETTE в tmp_path, так что документный
конвейер строится с нуля и реально бьётся об монкепатченный llm._create.

solve.py биндит OUT именем при импорте (`from util import OUT`), поэтому
патч util.WORK его не трогает — без отдельного monkeypatch.setattr(solve,
"OUT", ...) фолт-тесты писали бы деградированный submission поверх
РЕАЛЬНОГО out/submission.json и out/run-report.json (найдено ревью раунда
1 по md5). isolated_workdir патчит solve.OUT и сама проверяет, что реальный
out/ не изменился ни байтом — страховка на все тесты, использующие фикстуру,
а не только на один.
"""

import json
from pathlib import Path

import anthropic
import httpx
import pytest

import llm
import solve
import util

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")
TEMPLATE = json.loads(Path("dataset/agentic-bank-public/submission_template.json").read_text())

_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
_RESP_429 = httpx.Response(429, request=_REQ)


@pytest.fixture
def isolated_workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "WORK", tmp_path / "work")
    monkeypatch.setattr(llm, "CACHE", tmp_path / "llm_cache")
    monkeypatch.setattr(llm, "CASSETTE", tmp_path / "cassette")
    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    out_dir = tmp_path / "out"
    monkeypatch.setattr(solve, "OUT", out_dir)

    real_out = Path("out")
    before = _snapshot(real_out)
    yield out_dir
    after = _snapshot(real_out)
    assert before == after, "фолт-тест задел РЕАЛЬНЫЙ out/ — изоляция solve.OUT не сработала"


def _snapshot(out_dir: Path) -> dict[str, bytes]:
    if not out_dir.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(out_dir.glob("*.json"))}


def _assert_submission_complete(out_dir: Path):
    sub = json.loads((out_dir / "submission.json").read_text())
    assert sorted(sub["answers"]) == sorted(TEMPLATE["answers"])
    for cells in sub["answers"].values():
        for cell in cells.values():
            assert cell["status"] in ("BREACH", "COMPLIANT")
            assert isinstance(cell["actual"], int | float)


def test_zero_budget_run_still_submittable(isolated_workdir, monkeypatch):
    """Экстрагирующий прогон без единого доступного вызова API: всё — фолбэки."""
    monkeypatch.setattr(llm, "_budget", {"spent_usd": 99.0, "ceiling_usd": 0.0})
    solve.main(PUBLIC_ZIP, facts_source="extracted")
    _assert_submission_complete(isolated_workdir)


def test_dead_network_mid_run(isolated_workdir, monkeypatch):
    calls = {"n": 0}

    def dying(**kw):
        calls["n"] += 1
        raise anthropic.APIConnectionError(request=None)

    monkeypatch.setattr(llm, "_create", dying)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    solve.main(PUBLIC_ZIP, facts_source="extracted")
    _assert_submission_complete(isolated_workdir)
    assert calls["n"] > 0  # монкепатч реально был на пути вызова, не в обход кэша
    # провал не отравил кэш
    assert not any("error" in p.read_text() for p in llm.CACHE.glob("*.json"))


def test_429_storm_backs_off_and_caps(monkeypatch):
    # Тест сам эмулирует сеть через монкепатч _create — LLM_OFFLINE=1
    # (`make eval-offline`) иначе перехватывает вызов раньше, на проверке
    # кассеты/кэша, и тест ловит CassetteMiss вместо RateLimitError.
    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    sleeps = []
    attempts = {"n": 0}

    def limited(**kw):
        attempts["n"] += 1
        raise anthropic.RateLimitError("429", response=_RESP_429, body=None)

    monkeypatch.setattr(llm, "_create", limited)
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    with pytest.raises(anthropic.RateLimitError):
        llm.call("p", {"type": "object"}, "v-faults")
    assert attempts["n"] == 4  # потолок попыток
    assert sleeps == sorted(sleeps)  # backoff растёт
