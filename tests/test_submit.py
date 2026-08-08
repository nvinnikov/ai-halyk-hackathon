"""Снапшот отправки (раздел 3): submission/кэш/run-report под номером N,
диф изменившихся ячеек против предыдущего снапшота."""

import json
import os

import pytest

import submit
import util


@pytest.fixture(autouse=True)
def isolated_out(tmp_path, monkeypatch):
    out = tmp_path / "out"
    work = tmp_path / "work"
    out.mkdir()
    (work / "llm_cache").mkdir(parents=True)
    monkeypatch.setattr(util, "OUT", out)
    monkeypatch.setattr(util, "WORK", work)
    return out, work


def _write_submission(out, answers):
    (out / "submission.json").write_text(json.dumps({"answers": answers}))


def test_first_snapshot_numbered_1(isolated_out):
    out, work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    (work / "llm_cache" / "abc.json").write_text("{}")
    n = submit.snapshot()
    assert n == 1
    assert (out / "submission-1.json").read_text() == (out / "submission.json").read_text()
    assert (out / "cache-1" / "abc.json").exists()


def test_numbers_increment_past_existing_snapshots(isolated_out):
    out, _work = isolated_out
    (out / "submission-1.json").write_text("{}")
    (out / "submission-3.json").write_text("{}")
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    n = submit.snapshot()
    assert n == 4
    assert (out / "submission-4.json").exists()


def test_run_report_copied_alongside(isolated_out):
    out, _work = isolated_out
    _write_submission(out, {})
    (out / "run-report.json").write_text(json.dumps({"dataset_hash": "abc"}))
    submit.snapshot()
    assert json.loads((out / "run-report-1.json").read_text())["dataset_hash"] == "abc"


def test_missing_run_report_does_not_fail(isolated_out):
    out, _work = isolated_out
    _write_submission(out, {})
    submit.snapshot()  # run-report.json отсутствует — не должно падать
    assert not (out / "run-report-1.json").exists()


def test_missing_cache_dir_does_not_fail(isolated_out, tmp_path):
    out, work = isolated_out
    (work / "llm_cache").rmdir()
    _write_submission(out, {})
    n = submit.snapshot()
    assert n == 1
    assert not (out / "cache-1").exists()


def test_first_snapshot_has_no_diff_output(isolated_out, capsys):
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    submit.snapshot()
    assert "diff vs" not in capsys.readouterr().out


def test_diff_reports_changed_cells(isolated_out, capsys):
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT", "actual": 1.0}}})
    submit.snapshot()
    _write_submission(out, {"P1": {"6.1": {"status": "BREACH", "actual": 2.0}}})
    submit.snapshot()
    captured = capsys.readouterr().out
    assert "P1.6.1" in captured
    assert "1 изменённых ячеек" in captured


def test_diff_silent_when_answers_unchanged(isolated_out, capsys):
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT", "actual": 1.0}}})
    submit.snapshot()
    submit.snapshot()  # тот же submission.json второй раз
    captured = capsys.readouterr().out
    assert "0 изменённых ячеек" in captured


# --- отказ снимать снапшот с публичного прогона (ревью PR #18, круг 3) -------
#
# Гейты Makefile закрывают вход, но публичный прогон — штатный путь: `make
# solve` и `make determinism` его прямо предполагают. Оба оставляют в
# out/submission.json результат по публичному набору, и следующий `make submit`
# снял бы его снапшотом как кандидата на отправку. Проверка на выходе — одна
# точка на все пути перезаписи разом.
#
# Вердикт берётся готовым из run-report (поле is_public_dataset, его пишет
# solve сравнением байтов леджера). Хранимый отпечаток тут не годится: ревью
# PR #18, круг 5 — eval/public_baseline.json перезаписывается
# `sanity.py <любой>.zip --write-baseline` и приватный хеш в нём штатно
# возможен.


def _report(out, **fields):
    (out / "run-report.json").write_text(json.dumps(fields))


@pytest.fixture(autouse=True)
def no_force(monkeypatch):
    """SUBMIT_FORCE снимается по умолчанию: иначе экспортированная в оболочке
    переменная молча переводила бы тесты отказа в проверку обхода — тот же
    класс, что унаследованный ARCHIVE в тестах гейтов (ревью PR #18, круг 3)."""
    monkeypatch.delenv("SUBMIT_FORCE", raising=False)


def test_snapshot_refuses_public_dataset_run(isolated_out, capsys):
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    _report(out, is_public_dataset=True)
    with pytest.raises(SystemExit):
        submit.snapshot()
    assert not (out / "submission-1.json").exists(), "снапшот публичного прогона всё-таки создан"
    printed = capsys.readouterr().out
    assert "ПУБЛИЧНОМУ НАБОРУ" in printed
    assert "SUBMIT_FORCE=1" in printed, "отказ не называет выход — под таймером его придётся придумывать"


def test_snapshot_force_overrides_refusal(isolated_out, capsys, monkeypatch):
    """Критерий под отказом — эвристика: solve._is_public_dataset сравнивает
    только байты леджера, а приватный пакет может приехать с тем же леджером и
    другими документами (ревью PR #18, круг 7). Отказ без обхода превратил бы
    ложное срабатывание в тупик: совет «перезапустите прогон» даст тот же
    вердикт, и выйти можно было бы только копированием файла руками.
    """
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    _report(out, is_public_dataset=True)
    monkeypatch.setenv("SUBMIT_FORCE", "1")
    assert submit.snapshot() == 1
    assert (out / "submission-1.json").exists()
    assert "принудительно" in capsys.readouterr().out


def test_snapshot_allows_private_dataset_run(isolated_out):
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    _report(out, is_public_dataset=False)
    assert submit.snapshot() == 1


def test_snapshot_proceeds_when_verdict_field_absent(isolated_out, capsys):
    """Отчёт от версии до этой правки поля не несёт — это «не установлено», а
    не «не публичный»: молчаливое «не публичный» пропустило бы ровно тот
    сценарий, ради которого проверка и вводилась."""
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    _report(out, dataset_hash="03b886a4f357722e")
    assert submit.snapshot() == 1
    assert "происхождение прогона не установлено" in capsys.readouterr().out


def test_snapshot_proceeds_when_run_report_older_than_submission(isolated_out, capsys):
    """Отчёт старше submission — он от ПРОШЛОГО прогона (ревью PR #18, круг 4).

    solve пишет отчёт последним, а скелет submission — первым, поэтому
    прерванный прогон штатно оставляет пару «свежий submission + отчёт
    прошлого прогона». Судить по такому отчёту нельзя ни в какую сторону:
    вердикт от репетиции по публичному архиву обернулся бы отказом снять
    снапшот с приватных ответов упавшего боевого прогона, а ранбук в этом
    месте велит снимать снапшот немедленно.
    """
    out, _work = isolated_out
    _report(out, is_public_dataset=True)
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    os.utime(out / "run-report.json", (1_000_000, 1_000_000))
    os.utime(out / "submission.json", (2_000_000, 2_000_000))
    assert submit.snapshot() == 1
    assert "происхождение прогона не установлено" in capsys.readouterr().out


def test_snapshot_proceeds_when_run_report_unreadable(isolated_out, capsys):
    """Fail-open: происхождение прогона не установлено — это не повод не дать
    снять снапшот. Отправленная работа лучше неотправленной, а неизвестность
    громко печатается."""
    out, _work = isolated_out
    _write_submission(out, {"P1": {"6.1": {"status": "COMPLIANT"}}})
    (out / "run-report.json").write_text("{битый json")
    assert submit.snapshot() == 1
    assert "происхождение прогона не установлено" in capsys.readouterr().out
