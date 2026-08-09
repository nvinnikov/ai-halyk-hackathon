"""Валютная нормализация (5.5.1): в USD при загрузке, до любой агрегации.

Направление закодировано в имени поля: сумма в валюте умножается на
usd_per_unit. Лестница фолбэка: свой курс → курс любого другого целевого
заёмщика (тай-брейк: последний по дате документа, при равных — по
возрастанию хеша) → строка исключается с алярмом. Подстановка 1.0 не
является ступенью лестницы ни при каких условиях: неизвестный курс — это
отсутствие суммы, а не сумма один к одному.

Округления здесь нет: конвертированная строка несёт полную точность
произведения, копейки режет только вывод (util.q2). Промежуточный quantize
дал бы разный итог у «сконвертировать и сложить» и «сложить и
сконвертировать».

Порядок относительно фактов досье: стадия идёт ДО engine.prepare_rows,
поэтому amount_override перекрывает уже сконвертированную сумму и курсом
не домножается. Это осознанный контракт: записка казначейства фиксирует
итоговую долларовую сумму операции, а не сумму в валюте платежа.
"""

from decimal import Decimal, InvalidOperation


def _value(rate: dict) -> Decimal | None:
    """Курс как Decimal; None — если значение не разобрать или оно не
    положительно. Такой курс не курс: '0' обнулил бы строку, а мусор из
    извлечения уронил бы сценарий целиком. Он выбывает из кандидатов, и
    лестница идёт дальше — вплоть до fx_uncovered_row. Молчаливой 1.0 нет
    и здесь.
    """
    try:
        v = Decimal(str(rate.get("usd_per_unit")))
    except (InvalidOperation, ValueError):
        return None
    return v if v.is_finite() and v > 0 else None


def _covers(rate: dict, date: str) -> bool:
    frm = rate.get("effective_from") or "0000-00-00"
    to = rate.get("effective_to") or "9999-99-99"
    return frm <= date <= to


def _unbounded(rate: dict) -> bool:
    """Курс без хотя бы одной границы интервала действует всегда — 5.5.1
    требует пометить это в трейсе, а не молча растянуть период."""
    return not rate.get("effective_from") or not rate.get("effective_to")


def _candidate(rate: dict) -> dict:
    return {
        "usd_per_unit": rate["usd_per_unit"],
        "source_quote": rate.get("source_quote", ""),
        "doc_date": rate.get("doc_date", ""),
        "doc_hash": rate.get("doc_hash", ""),
    }


def pick_rate(rates: list[dict], currency: str, date: str) -> dict | None:
    """Курс валюты на дату операции; None — если интервалы дату не накрывают.

    Разрешение конфликта детерминировано и не зависит от порядка на входе:
    последний по дате документа, при равных — по возрастанию хеша, при
    равных и хешах — по возрастанию курса. Сортировка двухпроходная
    (устойчивая), а не одним ключом с reverse: инвертировать строку хеша
    ради обратного порядка — трюк, который ломается на не-hex хешах.
    """
    fit = [r for r in rates if r.get("currency") == currency and _covers(r, date) and _value(r) is not None]
    if not fit:
        return None
    fit.sort(key=lambda r: ((r.get("doc_hash") or ""), _value(r)))
    fit.sort(key=lambda r: r.get("doc_date") or "", reverse=True)
    picked = dict(fit[0])
    # Сравнение по Decimal, а не по строке: '1.16' и '1.1600' — один курс.
    picked["conflict"] = len({_value(r) for r in fit}) > 1
    picked["unbounded_interval"] = _unbounded(fit[0])
    picked["candidates"] = [_candidate(r) for r in fit]
    return picked


def _convert(row: dict, rate: dict) -> dict:
    rec = dict(row)
    rec["amt"] = row["amt"] * _value(rate)
    rec["currency"] = "USD"
    rec["fx_applied"] = rate["usd_per_unit"]
    rec["fx_source_quote"] = rate.get("source_quote", "")
    if rate.get("unbounded_interval"):
        rec["fx_unbounded_interval"] = True
    return rec


# Валюта расчёта. Контракт задачи задаёт её через usd_per_unit: строки леджера
# нормализуются сюда, и любое число, приезжающее в метрику из документа, обязано
# быть в ней же (ревью PR #23, пятая волна).
BASE_CURRENCY = "USD"


def to_usd(rows: list[dict], own_rates: list[dict], donor_rates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Строки заёмщика в USD плюс алярмы; непокрытая строка выбывает из расчёта."""
    out, alarms = [], []
    for r in sorted(rows, key=lambda x: x["txn_id"]):
        if r["currency"] == "USD":
            out.append(r)
            continue
        common = {"txn": r["txn_id"], "currency": r["currency"], "date": r["date"]}
        if r["amt"] is None:
            # Конвертировать нечего: сумму такой строке может вернуть только
            # amount_override, а он уже в USD — курс ей не нужен. Алярм всё
            # равно нужен: именно здесь допущение «override в долларах»
            # становится несущим, и на приватном наборе это должно быть видно.
            alarms.append({"kind": "fx_missing_amount_non_usd", **common})
            out.append({**r, "fx_skipped": "no_amount"})
            continue
        rate = pick_rate(own_rates, r["currency"], r["date"])
        donor = False
        if rate is None:
            rate = pick_rate(donor_rates, r["currency"], r["date"])
            donor = rate is not None
        if rate is None:
            alarms.append({"kind": "fx_uncovered_row", **common})
            continue
        if donor:
            alarms.append({"kind": "fx_donor_used", **common, "usd_per_unit": rate["usd_per_unit"]})
        if rate["conflict"]:
            # Полный список кандидатов — чтобы человек мог пересмотреть выбор,
            # не поднимая исходные документы.
            alarms.append({"kind": "fx_conflict", **common, "candidates": rate["candidates"]})
        if rate["unbounded_interval"]:
            alarms.append({"kind": "fx_unbounded_interval", **common, "usd_per_unit": rate["usd_per_unit"]})
        out.append(_convert(r, rate))
    return out, alarms


def coverage_alarms(rows: list[dict], own_rates: list[dict], donor_rates: list[dict]) -> list[dict]:
    """Проверка покрытия до расчёта: непокрытая валюта бьёт по заёмщику целиком."""
    missing = sorted(
        {
            (r["currency"], r["date"])
            for r in rows
            if r["currency"] != "USD"
            and r["amt"] is not None
            and pick_rate(own_rates, r["currency"], r["date"]) is None
            and pick_rate(donor_rates, r["currency"], r["date"]) is None
        }
    )
    return [{"kind": "fx_uncovered", "currency": c, "date": d} for c, d in missing]
