"""Второй ярус категоризации через LLM для непокрытых описаний."""

import pytest

import llm
from categorize_llm import categorize_batch
from ledger import load_ledger


class FakeUsage:
    input_tokens = 100
    output_tokens = 10


class FakeBlock:
    type = "tool_use"

    def __init__(self, data):
        self.input = data


class FakeResp:
    usage = FakeUsage()

    def __init__(self, data, stop_reason="tool_use"):
        self.content = [FakeBlock(data)]
        self.stop_reason = stop_reason


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE", tmp_path / "llm_cache")
    monkeypatch.setattr(llm, "CASSETTE", tmp_path / "cassette")
    monkeypatch.setattr(llm, "_budget", {"spent_usd": 0.0, "ceiling_usd": 10.0})
    monkeypatch.delenv("LLM_OFFLINE", raising=False)
    import util

    monkeypatch.setattr(util, "WORK", tmp_path)


def test_categorize_batch_basic(monkeypatch):
    """Монкепатч llm.call: батч из 3 описаний размечается, результат — (словарь, алярмы)."""
    call_count = [0]

    def fake_call(prompt, schema, schema_version, document_b64=None, max_tokens=8000):
        call_count[0] += 1
        # Проверяем что промпт содержит описания
        assert "Sewer discharge levy" in prompt
        assert "Electricity bills" in prompt
        assert "Payroll advance" in prompt
        return {
            "categories": [
                {"description": "Sewer discharge levy", "category": "UTILITIES"},
                {"description": "Electricity bills", "category": "UTILITIES"},
                {"description": "Payroll advance", "category": "PAYROLL"},
            ]
        }

    monkeypatch.setattr(llm, "call", fake_call)

    descriptions = ["Sewer discharge levy", "Electricity bills", "Payroll advance"]
    mapping, alarms = categorize_batch(descriptions)

    assert mapping == {
        "Sewer discharge levy": "UTILITIES",
        "Electricity bills": "UTILITIES",
        "Payroll advance": "PAYROLL",
    }
    assert alarms == []
    assert call_count[0] == 1


def test_categorize_batch_out_of_taxonomy_stays_other(monkeypatch):
    """Ответ с категорией вне таксономии → описание остаётся OTHER, алярм category_rejected."""

    def fake_call(prompt, schema, schema_version, document_b64=None, max_tokens=8000):
        return {
            "categories": [
                {"description": "Unknown transaction", "category": "UNKNOWN_CATEGORY"},
            ]
        }

    monkeypatch.setattr(llm, "call", fake_call)

    descriptions = ["Unknown transaction"]
    mapping, alarms = categorize_batch(descriptions)

    # Описание остаётся OTHER (вне таксономии)
    assert mapping == {"Unknown transaction": "OTHER"}
    # Проверяем алярм category_rejected
    assert len(alarms) == 1
    assert alarms[0]["kind"] == "category_rejected"
    assert alarms[0]["description"] == "Unknown transaction"
    assert alarms[0]["returned"] == "UNKNOWN_CATEGORY"


def test_categorize_batch_roundtrip_missing_and_rewritten(monkeypatch):
    """Ответ сверяется с батчем: пропущенное описание — алярм category_missing,
    переписанное моделью — category_unmatched_description, не молчаливый OTHER."""

    def fake_call(prompt, schema, schema_version, document_b64=None, max_tokens=8000):
        return {
            "categories": [
                {"description": "Electricity bills", "category": "UTILITIES"},
                {"description": "Electricity bills (rewritten)", "category": "UTILITIES"},
            ]
        }

    monkeypatch.setattr(llm, "call", fake_call)
    mapping, alarms = categorize_batch(["Electricity bills", "Payroll advance"])
    assert mapping == {"Electricity bills": "UTILITIES"}
    kinds = sorted(a["kind"] for a in alarms)
    assert kinds == ["category_missing", "category_unmatched_description"]
    missing = next(a for a in alarms if a["kind"] == "category_missing")
    assert missing["description"] == "Payroll advance"


def test_categorize_batch_deterministic_sorting(monkeypatch):
    """Детерминизм: batching режет отсортированный список уникальных описаний."""
    batches = []

    def fake_call(prompt, schema, schema_version, document_b64=None, max_tokens=8000):
        # Сохраняем порядок описаний из промпта
        import re

        descriptions_in_prompt = re.findall(r"^(\d+\. .+)$", prompt, re.MULTILINE)
        batches.append(list(descriptions_in_prompt))
        return {
            "categories": [
                {"description": line.split(". ", 1)[1], "category": "REVENUE"}
                for line in descriptions_in_prompt
            ]
        }

    monkeypatch.setattr(llm, "call", fake_call)

    # Передаём описания в перемешанном порядке
    descriptions = ["Zebra transaction", "Apple payment", "Mango sale"]
    categorize_batch(descriptions)

    # Проверяем что внутри батча описания отсортированы
    assert len(batches) == 1


def test_categorize_batch_batching_by_50(monkeypatch):
    """Батчинг по 50: много описаний режутся на части."""
    call_count = [0]

    def fake_call(prompt, schema, schema_version, document_b64=None, max_tokens=8000):
        call_count[0] += 1
        # Подсчитаем количество строк в промпте (приблизительно)
        import re

        lines_with_desc = re.findall(r"^(\d+\. .+)$", prompt, re.MULTILINE)
        assert len(lines_with_desc) <= 50, f"Батч содержит {len(lines_with_desc)} описаний, ожидается <= 50"
        # Возвращаем ответ с реальными описаниями
        return {
            "categories": [
                {"description": line.split(". ", 1)[1], "category": "REVENUE"} for line in lines_with_desc
            ]
        }

    monkeypatch.setattr(llm, "call", fake_call)

    # Создаём 75 уникальных описаний (потребует 2 батча)
    descriptions = [f"Transaction {i}" for i in range(75)]
    categorize_batch(descriptions)

    # Проверяем что было несколько вызовов llm.call
    assert call_count[0] > 1


def test_load_ledger_adds_cat_tier_after_categorize(tmp_path, monkeypatch):
    """load_ledger добавляет cat_tier: 1 (правила) или 2 (LLM, только для целевых)."""
    import csv
    import io
    import zipfile

    import util
    from ledger import extract_archive

    monkeypatch.setattr(util, "WORK", tmp_path)

    # Создаём архив с CSV
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("dataset/submission_template.json", "{}")

        # CSV с описаниями: одно покроется правилами (payroll),
        # одно не покроется (будет OTHER). Вторая строка содержит целевой id в txn_id.
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["txn_id", "date", "account_id", "counterparty", "description", "amount", "currency"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "txn_id": "TXN-001",
                "date": "2025-01-01",
                "account_id": "ACC-001",
                "counterparty": "Corp",
                "description": "payroll",
                "amount": "100.00",
                "currency": "USD",
            }
        )
        writer.writerow(
            {
                "txn_id": "SCENARIO-A_TXN-002",  # Содержит целевой id SCENARIO-A
                "date": "2025-01-02",
                "account_id": "ACC-001",
                "counterparty": "Corp",
                "description": "Unknown type of charge",
                "amount": "200.00",
                "currency": "USD",
            }
        )
        z.writestr("dataset/ledger.csv", output.getvalue())

    ds_hash, input_dir = extract_archive(archive)

    # Монкепатчим llm.call для тестирования второго яруса
    def fake_llm_call(prompt, schema, schema_version, document_b64=None, max_tokens=8000):
        return {
            "categories": [
                {"description": "Unknown type of charge", "category": "UTILITIES"},
            ]
        }

    monkeypatch.setattr(llm, "call", fake_llm_call)

    wd = tmp_path / ds_hash
    # Передаём целевой сценарий
    art = load_ledger(wd, input_dir, target_scenarios=["SCENARIO-A"])

    rows = art["rows"]
    # Первая строка покрыта правилами → cat_tier == 1
    payroll_row = next(r for r in rows if r["txn_id"] == "TXN-001")
    assert payroll_row["cat"] == "PAYROLL"
    assert payroll_row["cat_tier"] == 1

    # Вторая строка покрыта LLM → cat_tier == 2 (только потому что это целевой сценарий)
    utility_row = next(r for r in rows if "SCENARIO-A" in r["txn_id"])
    assert utility_row["cat"] == "UTILITIES"
    assert utility_row["cat_tier"] == 2


def test_ledger_version_incremented(monkeypatch):
    """Схема артефакта менялась — версия обязана расти, иначе на диске останется
    кэш прошлой схемы: 3 — категории второго яруса, 4 — категория у строк с
    неразобранной суммой."""
    from ledger import LEDGER_VERSION

    assert LEDGER_VERSION >= 4


def test_categorize_batch_llm_failure_is_fail_open(monkeypatch):
    """Сбой LLM (кассета, бюджет, сеть) стоит батча OTHER с алярмом, не прогона."""

    def boom(*a, **k):
        raise llm.CassetteMiss("нет кассеты")

    monkeypatch.setattr(llm, "call", boom)
    mapping, alarms = categorize_batch(["Some payment"])
    assert mapping == {}
    assert any(a["kind"] == "categorize_failed" for a in alarms)


def test_order_changes_prompt_not_semantics(monkeypatch):
    """Три перестановки дают три разных промпта при одном наборе описаний."""
    import categorize_llm

    seen = []

    def fake_call(prompt, schema, version, **kw):
        seen.append(prompt)
        return {"categories": [{"description": d, "category": "UTILITIES"} for d in descs]}

    # 4 описания, не 3: с тремя "Zebra/Alpha/Mango levy" hash-порядок случайно
    # совпадает с reverse-порядком (sha256 — не random, совпадение стабильное),
    # и тест был бы вечно красным по вине сэмпла, а не кода.
    descs = ["Zebra levy", "Alpha levy", "Mango levy", "Delta levy"]
    monkeypatch.setattr(categorize_llm.llm, "call", fake_call)
    for order in ("sorted", "reverse", "hash"):
        categorize_llm.categorize_batch(descs, order=order)
    assert len(set(seen)) == 3, "перестановки обязаны давать разные промпты"


def test_order_default_is_sorted(monkeypatch):
    """Дефолт рабочего пути не меняется."""
    import categorize_llm

    seen = []

    def fake_call(prompt, schema, version, **kw):
        seen.append(prompt)
        return {"categories": []}

    monkeypatch.setattr(categorize_llm.llm, "call", fake_call)
    categorize_llm.categorize_batch(["B item", "A item"])
    categorize_llm.categorize_batch(["B item", "A item"], order="sorted")
    assert seen[0] == seen[1]


def test_unknown_order_rejected():
    import pytest as _pytest

    import categorize_llm

    with _pytest.raises(ValueError):
        categorize_llm.categorize_batch(["x"], order="random")


def test_unknown_order_rejected_on_empty_input():
    """Пустой список не должен маскировать опечатку в имени перестановки —
    иначе замер разброса (5.2.1) тихо получит два одинаковых прогона вместо трёх."""
    import pytest as _pytest

    import categorize_llm

    with _pytest.raises(ValueError):
        categorize_llm.categorize_batch([], order="bogus")
