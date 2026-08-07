"""Ни одного имени заёмщика, порога или номера пункта вне tests/ и eval/."""

from pathlib import Path

from grep_gate import forbidden_literals, scan


def test_forbidden_list_is_substantial():
    lits = forbidden_literals()
    assert "TXN-" in lits and "ACC-" in lits
    assert any("Ertis" in x for x in lits)


def test_planted_literal_caught(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("threshold = 4_000_000  # P3 trigger\n")
    hits = scan([bad])
    assert hits and {h["literal"] for h in hits} & {"4_000_000", "P3"}


def test_planted_name_caught(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("RELATED = ['Ertis Capital LLP']\n")
    hits = scan([bad])
    assert any(h["literal"] == "Ertis Capital LLP" for h in hits)


def test_scoring_weights_not_forbidden(tmp_path):
    """0.50/0.30/0.20/0.05 стоят в score.py по определению официальной формулы
    и порогами ковенантов не являются — иначе гейт потребовал бы «починить»
    скорер."""
    ok = tmp_path / "score_like.py"
    ok.write_text("STATUS, ACTUAL, EVIDENCE, TOL = 0.50, 0.30, 0.20, 0.05\n")
    assert scan([ok]) == []


def test_space_grouped_threshold_caught(tmp_path):
    """Русская типографика пишет деньги пробелом. Форма `$4 000 000` в тексте
    промпта — ровно тот якорь, ради которого гейт и существует, и он проползал
    мимо, пока искались только 4000000/4_000_000/4,000,000."""
    bad = tmp_path / "prompt.py"
    bad.write_text('PROMPT = "пример: поступления превышают $4 000 000"\n')
    hits = scan([bad])
    assert any(h["literal"] == "4 000 000" for h in hits)


def test_comment_and_docstring_are_not_code(tmp_path):
    """Гейт стережёт знание, влияющее на поведение. Пояснение человеку в
    комментарии или докстринге в модель не уходит и вычислением не читается;
    строковый литерал промпта — уходит, и остаётся под гейтом."""
    ok = tmp_path / "explained.py"
    ok.write_text(
        '"""Порог 9.00 у P5 разобран отдельно."""\n\n'
        "def f():\n"
        '    """Пример: TXN-B1-0020 реклассифицирована."""\n'
        "    return 1  # сравнить с 500_000\n"
    )
    assert scan([ok]) == []

    bad = tmp_path / "prompted.py"
    bad.write_text('PROMPT = "порог 9.00"\n')
    # «9.00» содержит «9.0», и запрещены обе формы — ловятся обе.
    assert {h["literal"] for h in scan([bad])} == {"9.0", "9.00"}


def test_solution_is_clean():
    files = sorted(Path("solution").glob("*.py")) + [Path("run.sh")]
    assert scan(files) == []
