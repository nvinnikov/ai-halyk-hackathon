"""Гейт фундамента: run.sh на публичном архиве воспроизводит 34.00,
submission валиден на любой секунде прогона, ячейка падает — прогон нет.

Все вызовы solve.main здесь передают facts_source="expected" явно: задача 24
сменит дефолт на "extracted", и неявные вызовы молча стали бы боевыми
LLM-прогонами.
"""

import json
from pathlib import Path

import pytest

import llm
import solve
from score import score

PUBLIC_ZIP = Path("6a741640c31eb032062683.zip")
GT = json.loads(Path("dataset/agentic-bank-public/ground_truth.json").read_text())["scenarios"]
TEMPLATE = json.loads(Path("dataset/agentic-bank-public/submission_template.json").read_text())
BASELINE = 34.00
# Потолок фундамента по LOBO-замеру: скор выше — признак подгонки под публичный
# ключ, а не улучшения. Легитимный рост потолка поднимается осознанно, тем же
# коммитом, что и его причина.
MAX_SCORE = 34.00
CELL_FIELDS = ("actual", "evidence_txn_id", "status")


def assert_cell_valid(cell: dict, where: str) -> None:
    """Форма ячейки. dump_submission сверяет с шаблоном только пары
    (сценарий, пункт), поля внутри ячейки не проверяет никто, кроме этого."""
    assert sorted(cell) == list(CELL_FIELDS), f"{where}: поля {sorted(cell)}"
    assert cell["status"] in ("BREACH", "COMPLIANT"), f"{where}: статус {cell['status']!r}"
    assert isinstance(cell["actual"], int | float) and not isinstance(cell["actual"], bool), (
        f"{where}: actual не число ({cell['actual']!r})"
    )
    assert cell["evidence_txn_id"] is None or isinstance(cell["evidence_txn_id"], str), (
        f"{where}: улика не строка и не None ({cell['evidence_txn_id']!r})"
    )


@pytest.fixture(scope="module")
def answers():
    return solve.main(PUBLIC_ZIP, facts_source="expected")


def test_score_not_below_baseline(answers):
    total = score(answers, GT, verbose=True)
    assert total >= BASELINE, f"скор упал: {total:.2f} < {BASELINE:.2f}"
    assert total <= MAX_SCORE + 1e-9, f"скор выше потолка: {total:.2f} > {MAX_SCORE:.2f} — подгонка?"


def test_hash_printed_first(capsys):
    solve.main(PUBLIC_ZIP, facts_source="expected")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("dataset_hash: ")
    # Провайдер/модель — сразу следующей строкой (ревью PR #12, круг 4):
    # LLM_PROVIDER переключает бэкенд молча, без этой строки не видно, каким
    # провайдером реально шёл прогон.
    assert lines[1].startswith("provider: ")


def test_template_cells_have_expected_fields():
    """CELL_FIELDS — не выдумка теста, а форма ячейки из шаблона организаторов."""
    for sc, cells in TEMPLATE["answers"].items():
        for clause, cell in cells.items():
            assert sorted(cell) == list(CELL_FIELDS), f"шаблон {sc} {clause}: поля {sorted(cell)}"


def test_submission_file_matches_template(answers, isolated_out):
    sub = json.loads((isolated_out / "submission.json").read_text())
    assert sorted(sub["answers"]) == sorted(TEMPLATE["answers"])
    for sc, cells in sub["answers"].items():
        assert sorted(cells) == sorted(TEMPLATE["answers"][sc])
        for clause, cell in cells.items():
            assert_cell_valid(cell, f"{sc} {clause}")


def test_cell_failure_does_not_kill_run(monkeypatch):
    """Сломанное вычисление не убивает прогон: ячейка приходит по лестнице,
    и прочитанный порог не выбрасывается (5.7) — actual равен порогу спеки,
    а не медиане и не 1.0."""
    from decimal import Decimal

    from expected_extraction import SPECS

    import evidence

    def sabotaged(raw, facts, cellspec, overrides=None, set_exclude=frozenset()):
        raise RuntimeError("искусственный сбой вычисления")

    monkeypatch.setattr(evidence, "compute", sabotaged)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for sc, cells in answers.items():
        for clause, cell in cells.items():
            assert_cell_valid(cell, f"{sc} {clause}")
            assert cell["actual"] == float(Decimal(str(SPECS[sc][clause][2])))


def test_diagnostics_failure_does_not_kill_run(monkeypatch):
    """Диагностика (borrower-трейс, sign_divergence) — не расчёт: её падение
    не должно ни убивать прогон, ни отбрасывать уже посчитанную ячейку.
    После задачи 24 категории приходят от LLM, и expand() внутри
    sign_divergence может бросить KeyError на первой же невалидной."""

    def boom(*a, **k):
        raise KeyError("категория вне таксономии")

    monkeypatch.setattr(solve, "sign_divergence", boom)
    monkeypatch.setattr(solve, "_write_borrower_trace", boom)
    monkeypatch.setattr(solve, "cell_other_alarm", boom)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    total = score(answers, GT, verbose=False)
    assert total >= BASELINE  # ячейки посчитаны, диагностика потеряна — не наоборот


def test_unknown_scenario_facts_do_not_kill_run(monkeypatch):
    """Сценарий без фактов в эталоне (приватный набор): расчёт идёт по строкам
    без документальных решений, остальные сценарии не страдают."""
    victim = sorted(TEMPLATE["answers"])[0]
    # solve читает эталон лениво (импорт внутри _expected_facts), поэтому
    # правится сам словарь модуля — это тот же объект, что вернёт solve.
    from expected_extraction import FACTS

    monkeypatch.delitem(FACTS, victim)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for clause, cell in answers[victim].items():
        assert_cell_valid(cell, f"{victim} {clause}")
    other = sorted(TEMPLATE["answers"])[1]
    assert any(cell["evidence_txn_id"] is not None for cell in answers[other].values())


def test_scenario_load_failure_does_not_kill_run(monkeypatch):
    """Падение загрузки сценария не убивает прогон: его три ячейки остаются
    скелетом, остальные сценарии считаются."""
    victim = sorted(TEMPLATE["answers"])[0]
    original = solve.load_rows

    def sabotaged(scenario, all_rows, index, facts, donor_rates):
        if scenario == victim:
            raise RuntimeError("искусственный сбой загрузки сценария")
        return original(scenario, all_rows, index, facts, donor_rates)

    monkeypatch.setattr(solve, "load_rows", sabotaged)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for clause, cell in answers[victim].items():
        assert_cell_valid(cell, f"{victim} {clause}")
    other = sorted(TEMPLATE["answers"])[1]
    assert any(cell["evidence_txn_id"] is not None for cell in answers[other].values())


def test_trace_written_per_cell(answers):
    from ledger import extract_archive

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    traces = list((Path("work") / ds_hash / "trace").glob("*.json"))
    borrower = [t for t in traces if t.stem.endswith(".borrower")]
    assert len(traces) - len(borrower) == 36
    assert len(borrower) == 12


def test_deterministic(answers):
    assert answers == solve.main(PUBLIC_ZIP, facts_source="expected")


def _cell_traces() -> list[Path]:
    """Трейсы ячеек публичного прогона. Каталог адресуется отпечатком архива,
    иначе сюда попали бы трейсы мутированного прогона (задача 4)."""
    from util import dataset_hash, workdir

    return sorted((workdir(dataset_hash(PUBLIC_ZIP)) / "trace").glob("*.*.json"))


def test_other_unassigned_absent_on_public_set():
    """На публичном наборе OTHER пуст у всех целевых — алярма быть не должно.

    Тест держит границу: срабатывание здесь означает, что категоризация
    поехала, а не что алярм неверен.

    Прогон свой, а не из фикстуры `answers`: трейсы лежат на диске и хранят
    результат последнего вызова solve.main, кем бы он ни был сделан. Соседний
    test_diagnostics_failure_does_not_kill_run подменяет cell_other_alarm на
    падающую заглушку — после него в трейсах ни одного other_unassigned, и
    проверка прошла бы вакуумно. Кэш стадий делает свой вызов дешёвым."""
    solve.main(PUBLIC_ZIP, facts_source="expected")
    traces = _cell_traces()
    assert traces, "трейсы ячеек не найдены — прогон не состоялся"
    with_alarm = [t.name for t in traces if json.loads(t.read_text()).get("other_unassigned") is not None]
    assert with_alarm == [], f"неожиданный other_unassigned: {with_alarm}"


def test_other_unassigned_written_when_rows_lost(monkeypatch):
    """Строка, ушедшая в OTHER, обязана поднять алярм в трейсе ячейки."""
    original = solve.load_rows

    def lossy(scenario, all_rows, index, facts, donor_rates):
        raw, rows, alarms = original(scenario, all_rows, index, facts, donor_rates)
        # Первая строка выручки «не опозналась»: ровно тот промах, ради
        # которого алярм и вводится.
        for r in rows:
            if r["cat"] == "REVENUE":
                r["cat"] = "OTHER"
                break
        return raw, rows, alarms

    monkeypatch.setattr(solve, "load_rows", lossy)
    try:
        solve.main(PUBLIC_ZIP, facts_source="expected")
        hit = [
            a
            for a in (json.loads(t.read_text()).get("other_unassigned") for t in _cell_traces())
            if a is not None
        ]
        assert hit, "потерянная строка REVENUE не подняла ни одного алярма"
        assert all(a["other_sum"] != "0" for a in hit)
        # И в общий alarms: только его читают _alarm_counts и
        # invariants._collect_report_alarms, а в окне решают по run-report —
        # верхнего ключа трейса и строки в stdout для этого мало.
        in_alarms = [
            a
            for t in _cell_traces()
            for a in json.loads(t.read_text()).get("alarms", [])
            if a.get("kind") == "other_unassigned"
        ]
        assert in_alarms, "алярм не доехал до trace['alarms'] — run-report его не увидит"
        assert all(a.get("scenario") and a.get("clause") for a in in_alarms), (
            "без scenario/clause точный дедуп схлопнет срабатывания разных ячеек в одно"
        )
        # inputs_empty обязателен рядом с severity: severity=None означает
        # максимальную тяжесть, и сортировка run-report по сырому null уронила
        # бы такую ячейку вниз или упала бы с TypeError.
        assert all("inputs_empty" in a for a in in_alarms), (
            "нет признака inputs_empty — MAX-тяжесть в run-report неотличима от null"
        )
    finally:
        # Трейсы на диске общие: испорченный прогон обязан быть переписан
        # чистым, иначе соседний тест увидит чужой алярм.
        monkeypatch.undo()
        solve.main(PUBLIC_ZIP, facts_source="expected")


# --- реквизиты и run-report (задача 31) ---------------------------------------


def test_submission_meta_reads_env(monkeypatch):
    monkeypatch.setenv("TEAM_NAME", "команда Х")
    monkeypatch.setenv("CONTACT_EMAIL", "team@example.com")
    monkeypatch.setenv("MODEL_NAME", "custom-model")
    assert solve.submission_meta() == {
        "team": "команда Х",
        "contact_email": "team@example.com",
        "model": "custom-model",
    }


def test_submission_meta_defaults_model_to_llm_model(monkeypatch):
    # LLM_PROVIDER снимается явно: 9 августа он приходит из .env, и без этого
    # тест краснел бы от переменной окружения, а не от кода. Красный make check
    # в окне ранбук трактует как «откатиться на зелёный коммит» — ложный сигнал
    # здесь дороже самого теста.
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("TEAM_NAME", raising=False)
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    meta = solve.submission_meta()
    assert meta == {"team": "", "contact_email": "", "model": llm.MODEL}


def test_submission_meta_defaults_model_to_gemini_when_provider_gemini(monkeypatch):
    """Реквизиты не должны подписывать gemini-прогон anthropic-моделью
    (та же развилка провайдера, что в _build_run_report)."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    meta = solve.submission_meta()
    assert meta["model"] == llm.GEMINI_MODEL


def test_submission_meta_model_name_overrides_gemini_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "custom-model")
    meta = solve.submission_meta()
    assert meta["model"] == "custom-model"


def test_submission_written_to_out(answers, isolated_out):
    sub = json.loads((isolated_out / "submission.json").read_text())
    assert set(sub) == {"team", "contact_email", "model", "answers"}


def test_solve_out_isolated_from_real_out(answers, isolated_out):
    """Боевой out/ уходит организаторам — ни один тест не имеет права его
    перезаписать. Что он не изменился ни байтом, проверяет фикстура
    tests/conftest.py на выходе из каждого модуля; здесь фиксируется, что
    подмена вообще состоялась и запись ушла именно во временный каталог."""
    assert solve.OUT == isolated_out
    assert isolated_out.resolve() != Path("out").resolve()
    assert (isolated_out / "submission.json").is_file()


def test_run_report_written_with_expected_fields(answers, isolated_out):
    from ledger import extract_archive

    ds_hash, _ = extract_archive(PUBLIC_ZIP)
    report = json.loads((isolated_out / "run-report.json").read_text())
    assert report["dataset_hash"] == ds_hash
    assert len(report["archive_sha256"]) == 64  # полный sha256, не усечённый dataset_hash
    assert report["model"]
    assert "ledger.LEDGER_VERSION" in report["schema_versions"]
    assert "route.ROUTE_VERSION" in report["schema_versions"]
    assert "facts_extract.FACTS_VERSION" in report["schema_versions"]
    assert set(report["budget"]) == {"spent_usd", "ceiling_usd"}
    assert sum(report["tier_breakdown"].values()) == 36
    assert isinstance(report["alarm_counts"], dict)
    assert report["duration_s"] > 0
    # git_sha может быть None вне git-репозитория — поле обязано присутствовать
    assert "git_sha" in report


def test_alarm_counts_include_facts_and_specs_stage_alarms(tmp_path):
    """Правка по разбору инцидентов (задача 31): facts_extraction_failed/
    specs_extraction_failed запекаются ВНУТРЬ артефактов work/<hash>/facts,
    work/<hash>/specs (см. docs/ops/recovery-playbook.md) — их
    обязан видеть тот же счётчик, что и алярмы route/dossier/trace, иначе
    отравленный прогон в run-report выглядит чистым."""
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "ACC-1.json").write_text(
        json.dumps({"alarms": [{"kind": "facts_extraction_failed", "file": "a.pdf"}]})
    )
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "ACC-1.json").write_text(
        json.dumps({"alarms": [{"kind": "specs_extraction_failed", "error": "..."}]})
    )
    counts = solve._alarm_counts(tmp_path)
    assert counts.get("facts_extraction_failed") == 1
    assert counts.get("specs_extraction_failed") == 1


def test_run_report_failure_does_not_kill_run(monkeypatch):
    """Диагностика: падение сборки run-report не должно стоить ни одной ячейки."""

    def boom(*a, **k):
        raise RuntimeError("искусственный сбой run-report")

    monkeypatch.setattr(solve, "_build_run_report", boom)
    answers = solve.main(PUBLIC_ZIP, facts_source="expected")
    for sc, cells in answers.items():
        for clause, cell in cells.items():
            assert_cell_valid(cell, f"{sc} {clause}")


def test_stale_run_report_removed_with_skeleton(isolated_out, monkeypatch):
    """Отчёт прошлого прогона не переживает начало нового (ревью PR #18, круг 4).

    Отчёт пишется последним, скелет submission — первым, поэтому прерванный
    прогон оставлял на диске пару «свежий submission + отчёт прошлого
    прогона». submit.py судит о происхождении прогона по этому отчёту, и
    публичный хеш, оставшийся с репетиции, обернулся бы отказом снять снапшот
    с приватных ответов упавшего боевого прогона — прямо против точки
    принятия решений ранбука («прогон упал целиком → make submit немедленно»).
    Снятый отчёт означает «происхождение не установлено», а это fail-open.
    """
    stale = isolated_out / "run-report.json"
    stale.write_text(json.dumps({"dataset_hash": "отчёт-прошлого-прогона"}))

    def boom(*a, **k):
        raise RuntimeError("искусственный обрыв сразу после скелета")

    monkeypatch.setattr(solve, "load_ledger", boom)
    with pytest.raises(RuntimeError):
        solve.main(PUBLIC_ZIP, facts_source="expected")
    assert (isolated_out / "submission.json").exists(), "скелет не написан — тест проверяет не то"
    assert not stale.exists(), "отчёт прошлого прогона пережил начало нового"


def test_submission_meta_empty_requisites_alarm(monkeypatch, capsys):
    """Ревью PR #9 (12-я волна): забытые TEAM_NAME/CONTACT_EMAIL не молчат."""
    monkeypatch.delenv("TEAM_NAME", raising=False)
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    meta = solve.submission_meta()
    out = capsys.readouterr().out
    assert meta["team"] == "" and meta["contact_email"] == ""
    assert out.count("ALARM submission_meta_empty") == 2
