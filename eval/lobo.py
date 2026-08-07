"""LOBO (7.3): 12 прогонов, каждый раз один заёмщик решается без шаблонов.

После решения «out/net» (шапка плана) при сигнатурном матче исполняется DSL
самой спеки, а не альтернативная реализация: библиотека TEMPLATES и сырой
DSL спеки считают одно и то же после sign-нормализации. Поэтому ожидаемая
дельта LOBO у здорового заёмщика — около нуля, и это ХОРОШИЙ результат:
библиотека не подменяет извлечённое собственным прочтением. Ненулевая
дельта значит, что где-то в лестнице (heuristic_template, приор family/
by_clause/global) шаблон влияет на итог сильнее, чем спека, — это повод
разобрать конкретную ячейку, а не диагноз «шаблон подогнан под заёмщика»
в буквальном смысле. Порог delta > 0.5 — маркер «стоит посмотреть», не
приговор."""

import json
import sys
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

import solve
from score import score

GT_PATH = Path("dataset/agentic-bank-public/ground_truth.json")


def main(archive: Path) -> int:
    gt = json.loads(GT_PATH.read_text())["scenarios"]
    base = solve.main(archive, facts_source="extracted")
    worst = []
    for sc in sorted(gt):
        lobo = solve.main(archive, facts_source="extracted", hide_templates=frozenset({sc}))
        gt_one = {sc: gt[sc]}
        with_tpl = score({sc: base[sc]}, gt_one, verbose=False)
        without = score({sc: lobo[sc]}, gt_one, verbose=False)
        delta = with_tpl - without
        print(f"{sc:<4} с шаблонами {with_tpl:.2f}  без {without:.2f}  дельта {delta:+.2f}")
        if delta > 0.5:
            worst.append(sc)
    if worst:
        print(f"подогнанные шаблоны у: {worst}")
    return 1 if worst else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
