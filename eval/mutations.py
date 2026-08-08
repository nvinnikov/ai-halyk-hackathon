"""Мутации (7.2): переименование, сдвиг порогов и валюта. Ключ выводится без движка.

Дополняет `eval/mutations_text.py` (мутирует только текст, без прогона пайплайна).
Здесь — мутация ДАТАСЕТА целиком (новый архив → новый dataset_hash → новый
workdir) и сквозной прогон через `solve.main(..., facts_source="extracted")`.

Три мутации, и все три проверяются БЕЗ пересчёта нашим движком — иначе тест
измерял бы самосогласованность, а не правильность извлечения:

  rename — все имена компаний/контрагентов/дочек заменены по фиксированной
           таблице; ни одно число не меняется, ответы обязаны совпасть с
           немутированным прогоном байт в байт;
  shift  — порог одного пункта в тексте договора сдвинут (×0.72); новый
           статус выводится сравнением *старого* actual (из немутированного
           прогона) с новым порогом — predict_status, а не наш DSL;
  fx     — N строк леджера целевых заёмщиков переведены в EUR по известному
           курсу, курс вписан в текст документа казначейства; корректный
           пайплайн обязан восстановить исходные USD-суммы, поэтому ответы
           обязаны совпасть с немутированным прогоном.

Предзасев text/vision-артефактов. Документы (PDF) и структура zip не меняются
байт в байт, кроме подмены содержимого — переименование и есть единственная
мутация, трогающая CSV напрямую; shift и fx трогают только закэшированный
текст. Идемпотентность стадий (stages.artifact) пропускает извлечение и
подставляет предзасеянные артефакты; facts_extract/specs_extract остаются
некэшированными для мутированного текста — это и есть живой прогон.

Guard от холостой мутации: если замена (имя, порог, курс) не встретилась ни
разу ни в одном из мутированных текстов — RuntimeError, а не тихий зелёный
тест. Без этого «ответы не изменились» было бы зелёным при полностью
провалившейся замене.

Запуск: `uv run python eval/mutations.py <archive> [rename|shift|fx]`.
"""

import json
import re
import sys
import zipfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "solution")
sys.path.insert(0, "eval")

from expected_extraction import SPECS

from ledger import extract_archive, find_inputs
from util import dataset_hash, stable_json, workdir

# Фиксированная таблица замен первых значимых токенов имён; дополняется, если
# test_rename_map_covers_all_names найдёт непокрытое имя. Значения не
# пересекаются с ключами — иначе порядок применения дал бы двойную замену.
_RENAMES = {
    "Ertis": "Almaz",
    "Kazyna": "Orda",
    "Shymkent": "Kentau",
    "Aktau": "Balkash",
    "Zhetysu": "Merke",
    "Tien": "Alatau",
    "Turan": "Otrar",
    "Aral": "Esil",
    "Sarybel": "Koktal",
    "Taraz": "Sayram",
    "Atyrau": "Zaysan",
    "Syrdarya": "Tobol",
    "Ulytau": "Mangystau",
    "Zhezkazgan": "Stepnogorsk",
    "Saryarka": "Betpak",
    "Tengiz": "Karatau",
}

FX_RATE = Decimal("1.16")  # сколько USD за 1 EUR


@contextmanager
def isolated_solve_out(archive: Path):
    """solve.OUT биндится по имени при импорте (`from util import OUT` в
    solve.py), поэтому без подмены каждый прогон eval-скрипта писал бы
    submission.json/run-report.json поверх боевого out/ (тот же приём
    изоляции, что у фикстуры isolated_out в tests/conftest.py). Имя
    публичное: контекстом пользуются и lobo.py, и invariants.py. Каталог — подкаталог
    work/<hash своего архива>, а не общий tmp: у baseline и мутации разные
    dataset_hash, они не должны затирать друг друга при последовательных
    вызовах."""
    import solve

    prev = solve.OUT
    solve.OUT = workdir(dataset_hash(Path(archive))) / "eval-out"
    try:
        yield
    finally:
        solve.OUT = prev


def rename_map() -> dict[str, str]:
    return dict(_RENAMES)


def apply_renames(text: str, m: dict[str, str], hits: dict[str, int] | None = None) -> str:
    """Заменить токены по словарным границам; hits (если передан) накапливает
    число реальных попаданий по каждому ключу — основа guard'а от холостой
    мутации в build_renamed/build_fx."""
    for old, new in sorted(m.items()):
        pattern = rf"\b{re.escape(old)}\b"
        count = len(re.findall(pattern, text))
        if count:
            text = re.sub(pattern, new, text)
            if hits is not None:
                hits[old] = hits.get(old, 0) + count
    return text


def predict_status(gt_actual: float, direction: str, new_limit: float) -> str:
    """Статус по-новому порогу БЕЗ движка: сравнение уже посчитанного actual."""
    if direction == "max":
        return "BREACH" if gt_actual > new_limit else "COMPLIANT"
    return "BREACH" if gt_actual < new_limit else "COMPLIANT"


def _preseed_text_vision(pub_wd: Path, mut_wd: Path, mutate) -> None:
    """Копия text/vision-артефактов публичного workdir с mutate(doc_hash, text)
    применённой к тексту каждой страницы. mutate решает сама, трогать ли текст,
    и обязана быть идемпотентной (вызывается на каждой странице документа)."""
    for sub in ("text", "vision"):
        src = pub_wd / sub
        if not src.is_dir():
            continue
        dst = mut_wd / sub
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.glob("*.json")):
            art = json.loads(f.read_text())
            doc = f.stem.split(".")[0]
            if "pages" in art:
                for page in art["pages"]:
                    page["text"] = mutate(doc, page["text"])
            if "text" in art:
                art["text"] = mutate(doc, art["text"])
            (dst / f.name).write_text(stable_json(art))


_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # эпоха zip-формата, не время сборки


def _new_zip_from_root(root: Path, out_zip: Path, transform, marker: str | None = None) -> None:
    """Пересобрать zip из дерева root; transform(path, bytes) -> bytes решает,
    менять ли содержимое файла. Байты зависят только от СОДЕРЖИМОГО, не от
    времени сборки (ZipInfo с фиксированной эпохой — см. docs/ops/task-28-report.md).

    marker — детерминированная запись MUTATION.txt: мутации, не меняющие
    файлы датасета (shift/fx живут в предзасеянных text-артефактах), без неё
    давали бы ОДИНАКОВЫЕ байты → один dataset_hash → общий work/<hash>, и
    вторая цель считалась бы на закэшированных спеках первой (ревью PR #9,
    16-я волна)."""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w") as z:
        for p in sorted(root.rglob("*")):
            if p.is_dir():
                continue
            rel = str(p.relative_to(root.parent))
            info = zipfile.ZipInfo(rel, date_time=_ZIP_EPOCH)
            z.writestr(info, transform(p))
        if marker is not None:
            z.writestr(zipfile.ZipInfo("MUTATION.txt", date_time=_ZIP_EPOCH), marker)


def build_renamed(archive: Path) -> Path:
    """Новый zip с переименованными контрагентами в CSV + предзасев
    text/vision-артефактов теми же заменами. PDF-байты не трогаются."""
    pub_hash, input_dir = extract_archive(archive)
    m = rename_map()
    root = find_inputs(input_dir)["root"]
    out_zip = Path("work") / "mutated-renamed.zip"
    hits: dict[str, int] = {}

    def transform(p: Path) -> bytes:
        if p.suffix == ".csv":
            return apply_renames(p.read_text(), m, hits).encode()
        return p.read_bytes()

    _new_zip_from_root(root, out_zip, transform)

    mut_hash = dataset_hash(out_zip)
    mut_wd = workdir(mut_hash)
    pub_wd = workdir(pub_hash)
    _preseed_text_vision(pub_wd, mut_wd, lambda _doc, text: apply_renames(text, m, hits))

    missing = sorted(old for old in m if hits.get(old, 0) == 0)
    if missing:
        raise RuntimeError(f"mutation no-op: замены не встретились ни разу: {missing}")
    return out_zip


def _final_agreement_doc(pub_wd: Path, account: str) -> str:
    """doc_hash действующего договора счёта — по кэшу route/, не по имени файла."""
    candidates = []
    for f in sorted((pub_wd / "route").glob("*.json")):
        d = json.loads(f.read_text())
        if (
            d.get("account_id") == account
            and d.get("doc_type") == "agreement"
            and d.get("edition") == "final"
        ):
            candidates.append(d["doc_hash"])
    if len(candidates) != 1:
        raise RuntimeError(
            f"ожидался ровно один действующий договор для {account}, найдено {len(candidates)}"
        )
    return candidates[0]


_CLAUSE_BOUNDARY = re.compile(r"Пункт\s*\d+\.\d+|Статья\s*\d+")


def _shift_in_segment(text: str, clause: str, old: float, new: float) -> tuple[str, int]:
    """Заменить порог внутри сегмента СВОЕГО пункта (до следующего Пункт/Статья).
    Глобальная замена недопустима: то же число может стоять в другой статье."""
    start = re.search(rf"Пункт\s*{re.escape(clause)}\b", text)
    if not start:
        return text, 0
    rest = text[start.end() :]
    boundary = _CLAUSE_BOUNDARY.search(rest)
    seg_end = start.end() + (boundary.start() if boundary else len(rest))
    segment = text[start.start() : seg_end]
    for old_str in dict.fromkeys((f"{old:,.2f}", f"{old:.2f}")):
        count = segment.count(old_str)
        if count:
            new_str = f"{new:,.2f}"
            segment = segment.replace(old_str, new_str)
            return text[: start.start()] + segment + text[seg_end:], count
    return text, 0


def shift_threshold(archive: Path, scenario: str, clause: str) -> Path:
    """Копия датасета с порогом пункта `clause` сценария `scenario`, сдвинутым
    в тексте действующего договора на ×0.72 (пороги <100 — ставка/коэффициент,
    иначе — сумма, округляемая до тысяч)."""
    pub_hash, input_dir = extract_archive(archive)
    pub_wd = workdir(pub_hash)
    index = json.loads((pub_wd / "index.json").read_text())
    account = index["scenario_to_account"][scenario]
    target_doc = _final_agreement_doc(pub_wd, account)

    old = float(SPECS[scenario][clause][2])
    new = round(old * 0.72, 2) if old < 100 else round(old * 0.72, -3)

    root = find_inputs(input_dir)["root"]
    out_zip = Path("work") / f"mutated-shift-{scenario}-{clause.replace('.', '_')}.zip"
    _new_zip_from_root(root, out_zip, lambda p: p.read_bytes(), marker=f"shift {scenario} {clause} x0.72")

    mut_hash = dataset_hash(out_zip)
    mut_wd = workdir(mut_hash)
    hits = 0

    def mutate(doc: str, text: str) -> str:
        nonlocal hits
        if doc != target_doc:
            return text
        new_text, n = _shift_in_segment(text, clause, old, new)
        hits += n
        return new_text

    _preseed_text_vision(pub_wd, mut_wd, mutate)
    if hits == 0:
        raise RuntimeError(
            f"mutation no-op: порог {old} пункта {clause} сценария {scenario} не найден в тексте договора"
        )
    return out_zip


def _treasury_accounts(pub_wd: Path, target_accounts: set[str]) -> dict[str, str]:
    """account_id → doc_hash служебной записки казначейства, среди целевых счетов."""
    out: dict[str, str] = {}
    for f in sorted((pub_wd / "route").glob("*.json")):
        d = json.loads(f.read_text())
        if d.get("doc_type") == "treasury_memo" and d.get("account_id") in target_accounts:
            out.setdefault(d["account_id"], d["doc_hash"])
    return out


def build_fx(archive: Path, n_rows: int = 10) -> Path:
    """Копия датасета: N строк целевых заёмщиков в USD переведены в EUR по
    FX_RATE, курс вписан в текст их служебной записки казначейства.
    Корректный пайплайн восстанавливает исходные USD-суммы курсом из текста —
    ответы обязаны совпасть поячеечно с немутированным прогоном."""
    pub_hash, input_dir = extract_archive(archive)
    pub_wd = workdir(pub_hash)
    index = json.loads((pub_wd / "index.json").read_text())
    s2a = index["scenario_to_account"]
    treasury_by_account = _treasury_accounts(pub_wd, set(s2a.values()))
    if not treasury_by_account:
        raise RuntimeError("нет ни одного целевого счёта со служебной запиской казначейства")

    ledger_art = json.loads((pub_wd / "ledger.json").read_text())
    eligible = sorted(
        (
            r
            for r in ledger_art["rows"]
            if r["account_id"] in treasury_by_account and r["cat"] != "OTHER" and r["currency"] == "USD"
        ),
        key=lambda r: r["txn_id"],
    )
    picked = eligible[:n_rows]
    if not picked:
        raise RuntimeError("нет подходящих строк для fx-мутации")
    picked_ids = {r["txn_id"] for r in picked}
    touched_docs = {treasury_by_account[r["account_id"]] for r in picked}

    input_dir_ = input_dir
    ledger_csv = find_inputs(input_dir_)["ledger_csv"]
    root = find_inputs(input_dir_)["root"]
    out_zip = Path("work") / "mutated-fx.zip"
    row_hits = 0

    def transform(p: Path) -> bytes:
        nonlocal row_hits
        if p != ledger_csv:
            return p.read_bytes()
        text, n = _convert_rows_to_eur(p.read_text(), picked_ids)
        row_hits += n
        return text.encode()

    _new_zip_from_root(root, out_zip, transform)
    if row_hits != len(picked_ids):
        raise RuntimeError(f"mutation no-op: переведено {row_hits} строк из {len(picked_ids)}")

    mut_hash = dataset_hash(out_zip)
    mut_wd = workdir(mut_hash)
    rate_line = f" 5. Валютные курсы. Курс EUR: 1 EUR = {FX_RATE} USD, действует весь 2025 год."
    injected: set[str] = set()

    def mutate(doc: str, text: str) -> str:
        if doc in touched_docs and doc not in injected:
            injected.add(doc)
            return text.rstrip() + rate_line
        return text

    _preseed_text_vision(pub_wd, mut_wd, mutate)
    missing = touched_docs - injected
    if missing:
        raise RuntimeError(f"mutation no-op: курс не вписан в документы {sorted(missing)}")
    return out_zip


def _convert_rows_to_eur(csv_text: str, txn_ids: set[str]) -> tuple[str, int]:
    """Строки с txn_id из txn_ids: amount /= FX_RATE, currency = EUR. Полная
    точность деления (без округления до копеек) — конвертация обратно в
    fx.to_usd() восстанавливает исходную сумму с точностью до q2."""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames
    rows = list(reader)
    hits = 0
    for row in rows:
        if row.get("txn_id") in txn_ids:
            row["amount"] = str(Decimal(row["amount"]) / FX_RATE)
            row["currency"] = "EUR"
            hits += 1
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue(), hits


def _diff_answers(baseline: dict, mutated: dict) -> list[tuple[str, str, dict, dict | None]]:
    out = []
    for sc in sorted(baseline):
        for cl in sorted(baseline[sc]):
            want = baseline[sc][cl]
            got = mutated.get(sc, {}).get(cl)
            if got != want:
                out.append((sc, cl, want, got))
    return out


def main(archive: Path, which: str) -> bool:
    """Прогнать мутацию `which` через solve.main и сверить с немутированным
    прогоном. Возвращает True при совпадении (rename/fx — все ячейки,
    shift — все, кроме предсказанной); печатает расхождения при провале.
    Возврат bool, а не sys.exit, — чтобы main() можно было вызвать из теста."""
    import solve

    archive = Path(archive)
    with isolated_solve_out(archive):
        baseline = solve.main(archive, facts_source="extracted")

    if which == "rename":
        mutated_archive = build_renamed(archive)
        with isolated_solve_out(mutated_archive):
            mutated = solve.main(mutated_archive, facts_source="extracted")
        mismatches = _diff_answers(baseline, mutated)
        if mismatches:
            print(f"rename: расхождение в {len(mismatches)} ячейках:")
            for sc, cl, want, got in mismatches:
                print(f"  {sc} {cl}: было {want}, стало {got}")
            return False
        total = sum(len(v) for v in baseline.values())
        print(f"rename: OK, все {total} ячеек совпали")
        return True

    if which == "shift":
        scenario, clause = "B1", "6.1"
        gt_actual = baseline[scenario][clause]["actual"]
        direction = SPECS[scenario][clause][1]
        old = float(SPECS[scenario][clause][2])
        new = round(old * 0.72, 2) if old < 100 else round(old * 0.72, -3)
        expected_status = predict_status(gt_actual, direction, new)

        mutated_archive = shift_threshold(archive, scenario, clause)
        with isolated_solve_out(mutated_archive):
            mutated = solve.main(mutated_archive, facts_source="extracted")

        mismatches = []
        for sc in sorted(baseline):
            for cl in sorted(baseline[sc]):
                got = mutated.get(sc, {}).get(cl, {})
                if (sc, cl) == (scenario, clause):
                    if got.get("status") != expected_status:
                        mismatches.append((sc, cl, expected_status, got.get("status")))
                elif got != baseline[sc][cl]:
                    mismatches.append((sc, cl, baseline[sc][cl], got))
        if mismatches:
            print(f"shift: расхождение в {len(mismatches)} ячейках:")
            for sc, cl, want, got in mismatches:
                print(f"  {sc} {cl}: ожидалось {want}, получено {got}")
            return False
        print(f"shift {scenario}.{clause}: OK, порог {old} → {new}, статус {expected_status}")
        return True

    if which == "fx":
        mutated_archive = build_fx(archive)
        with isolated_solve_out(mutated_archive):
            mutated = solve.main(mutated_archive, facts_source="extracted")
        mismatches = _diff_answers(baseline, mutated)
        if mismatches:
            print(f"fx: расхождение в {len(mismatches)} ячейках:")
            for sc, cl, want, got in mismatches:
                print(f"  {sc} {cl}: было {want}, стало {got}")
            return False
        total = sum(len(v) for v in baseline.values())
        print(f"fx: OK, все {total} ячеек совпали")
        return True

    raise ValueError(f"неизвестная мутация {which!r}")


if __name__ == "__main__":
    ok = main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "rename")
    sys.exit(0 if ok else 1)
