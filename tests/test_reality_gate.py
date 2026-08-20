"""Гейт реальности: ручные правки финального окна как цель для кода.

9 августа шесть ячеек приватного набора ушли организаторам с ответами,
выведенными руками из леджера и договоров (два независимых пересчёта каждый,
разбор — docs/ops/attempt3-manual-edits-2026-08-09.md). Код эти ячейки пока
считает иначе — у каждой своя корневая причина, и у каждой причины свой пункт
роадмапа. Этот тест превращает ручное знание в измеримую цель: он гоняет
боевой конвейер по приватной реплике офлайн и сравнивает шесть ячеек с
целевыми ответами.

Форма смешанная и меняется по мере прогресса. Ячейка, которую код ещё не
умеет считать, помечена xfail(strict=False) поимённо в UNSOLVED — она даёт
XFAIL, а научившись, дала бы XPASS. Ячейка, которую код УЖЕ считает, из
UNSOLVED убрана, и её сравнение блокирующее: регрессия роняет тест, а не
возвращает его тихо в XFAIL. Маркер на всём наборе разом был бы дырой —
выигранная ячейка деградировала бы незаметно.

Закрытый набор хранится в git распакованным каталогом
(`dataset/agentic-bank-private/`), как и публичный, — он есть в любом чекауте.
Чего в git нет и что делает тест живым только на части машин — это прогретый
`work/<hash>/` с артефактами и `work/llm_cache/`: без них офлайн-прогон нечем
кормить, и тест скипается, в CI в том числе. Числа и идентификаторы TXN-/ACC-
здесь легальны: греп-гейт ограничивает solution/, а tests/ и eval/ —
разрешённые берега.
"""

import math
from pathlib import Path

import pytest

import solve
from private_archive import ARCHIVE as PRIVATE_ZIP
from private_archive import DATASET as PRIVATE_DATASET
from private_archive import build_private_archive
from util import dataset_hash


def _private_hash() -> str | None:
    """dataset_hash закрытого архива, собранного из dataset/agentic-bank-private/.

    Не хардкодится: он зависит от формата упаковки (tools/public_archive.
    PACK_FORMAT), а не только от содержимого датасета, — тот же приём, что и
    в tests/conftest.py для публичного архива. build_private_archive() — no-op,
    если архив уже собран текущим форматом."""
    if not PRIVATE_DATASET.is_dir():
        return None
    build_private_archive()
    return dataset_hash(PRIVATE_ZIP)


# Целевые ответы, выведенные руками из данных (формулы — в docs/ops):
# (сценарий, пункт, status, actual, evidence, корень расхождения с кодом)
REALITY = [
    # DSCR = (9512880.44−5204617.29)/(2190196.31+1298776.53) = 1.2348 < 1.25.
    # Корень: doc(principal_payments) — леджерная величина; чинит формульный
    # резолв (PR #27), после живого перезамера ячейка должна стать первой XPASS.
    ("H1", "6.1", "BREACH", 1.23, None, "леджерный doc-ключ principal_payments"),
    # Леверидж = 9617432.88/(3050142.55 + min(690314.22, 5% от 8240517.36))
    # = 2.778 < 3.00. Корень: кэп «5% of Revenue» — в DSL нет умножения.
    ("G1", "6.1", "COMPLIANT", 2.78, None, "кэп добавок «5% выручки» невыразим в DSL"),
    # Выручка 8240517.36 ≥ порога 7200000. Корень: quote_unverified уронил
    # валидную спеку, ячейка ушла на лестницу с порогом вместо значения.
    ("G1", "6.2", "COMPLIANT", 8240517.36, None, "quote_unverified на валидной спеке"),
    # CAPEX 2231849.65 + рекласс аудита 391258.30 (Temir) = 2623107.95 >
    # 2500000; рекласс переворачивает вердикт — он же улика. Корень:
    # quote_unverified + статус с приора на ярусе эвристики.
    ("S3", "6.2", "BREACH", 2623107.95, "TXN-S3-0054", "статус с приора при посчитанной метрике"),
    # (452521.99 + рекласс 217695.10)/(9358965.30−6399342.83) = 0.2265 > 0.20;
    # рекласс Kazyna переворачивает — улика. Корень: договор определяет EBITDA
    # как «Выручка − Операционные расходы», модель взяла роллап OPEX_TOTAL.
    ("J3", "6.2", "BREACH", 0.23, "TXN-J3-0021", "EBITDA договора против роллапа OPEX_TOTAL"),
    # Статья «Операционные расходы» = OTHER_OPEX: 22048853.45 < 25000000.
    # Корень: статья-роллап в извлечении + шаблон подменил категорию на CAPEX.
    ("X2", "6.1", "COMPLIANT", 22048853.45, None, "статья-роллап и подмена категории"),
]

# Допуск на actual — один процент: цель не «до копейки как руки», а «код
# считает ту же метрику» (формула та же, расхождение разве что в округлении
# и курсах). Скоринговый допуск 5% здесь был бы слишком щедрым: он пропустил
# бы «почти ту» метрику.
ACTUAL_RTOL = 0.01


def _replica_ready() -> bool:
    from util import workdir

    ds_hash = _private_hash()
    if ds_hash is None:
        return False
    wd = workdir(ds_hash)
    return (wd / "facts").is_dir() and (Path("work") / "llm_cache").is_dir()


@pytest.fixture(scope="module")
def private_answers(request):
    if not _replica_ready():
        pytest.skip(
            "приватной реплики нет (архив + прогретый work/ + llm_cache) — гейт реальности живёт только с ней"
        )
    mp = pytest.MonkeyPatch()
    request.addfinalizer(mp.undo)
    mp.setenv("LLM_OFFLINE", "1")  # ноль сетевых вызовов: артефакты и кэш
    mp.setenv("LLM_PROVIDER", "gemini")  # кэш прогрет под gemini
    return solve.main(PRIVATE_ZIP, facts_source="extracted")


def _cell_matches(cell: dict, status: str, actual: float, evidence: str | None) -> bool:
    if cell.get("status") != status:
        return False
    got = cell.get("actual")
    if not isinstance(got, int | float) or isinstance(got, bool):
        return False
    if not math.isclose(got, actual, rel_tol=ACTUAL_RTOL, abs_tol=0.01):
        return False
    return cell.get("evidence_txn_id") == evidence


# Ячейки, которые код ещё не считает сам. Маркер висит ПОИМЁННО, а не на всём
# наборе: как только ячейка научилась считаться, её имя отсюда убирается, и
# тест из отчётного превращается в блокирующий. Иначе выигранная ячейка тихо
# вернулась бы в XFAIL при регрессии, и гейт бы этого не заметил — ровно то,
# от чего он должен защищать.
UNSOLVED = {
    ("H1", "6.1"),  # плановые погашения не распознаны как агрегат леджера
    ("X2", "6.1"),  # шаблон подменяет статью операционных расходов капзатратами
}


@pytest.mark.parametrize(
    ("sc", "cl", "status", "actual", "evidence", "root"),
    [
        pytest.param(
            *row,
            marks=(
                [
                    pytest.mark.xfail(
                        strict=False, reason="код ещё не считает ячейку сам — см. роадмап в docs/ops"
                    )
                ]
                if (row[0], row[1]) in UNSOLVED
                else []
            ),
        )
        for row in REALITY
    ],
    ids=[f"{sc}-{cl}" for sc, cl, *_ in REALITY],
)
def test_reality_cell(private_answers, sc, cl, status, actual, evidence, root):
    cell = private_answers[sc][cl]
    assert _cell_matches(cell, status, actual, evidence), (
        f"{sc} {cl}: код {cell}, цель {status}/{actual}/ev={evidence} (корень: {root})"
    )


def test_reality_gate_summary(private_answers):
    """Форма ответа и сводка прогресса. Падает только на регрессии конвейера:
    сломался прогон или разъехалась форма — сводка сама по себе не ассертится."""
    cells = sum(len(v) for v in private_answers.values())
    assert cells == 84, f"реплика вернула {cells} ячеек вместо 84"
    done = [
        f"{sc} {cl}"
        for sc, cl, status, actual, evidence, _root in REALITY
        if _cell_matches(private_answers[sc][cl], status, actual, evidence)
    ]
    print(
        f"\nГЕЙТ РЕАЛЬНОСТИ: код сам считает {len(done)}/{len(REALITY)} ячеек ручного знания"
        + (f" ({', '.join(done)})" if done else "")
    )
