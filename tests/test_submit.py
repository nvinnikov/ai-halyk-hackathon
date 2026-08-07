"""Снапшот отправки (раздел 3): submission/кэш/run-report под номером N,
диф изменившихся ячеек против предыдущего снапшота."""

import json

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
