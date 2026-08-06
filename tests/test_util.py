"""dataset_hash — отпечаток входного архива, первая строка лога run.sh."""

from decimal import Decimal

from util import OUT, ROOT, WORK, dataset_hash, q2, stable_json


def test_dataset_hash_is_stable(tmp_path):
    a = tmp_path / "a.zip"
    a.write_bytes(b"payload")
    h = dataset_hash(a)
    assert h == dataset_hash(a)
    assert len(h) == 16 and int(h, 16) >= 0

    b = tmp_path / "b.zip"
    b.write_bytes(b"payload2")
    assert dataset_hash(b) != h


def test_workdir_is_under_hash(tmp_path, monkeypatch):
    import util

    monkeypatch.setattr(util, "WORK", tmp_path)
    d = util.workdir("abc123")
    assert d == tmp_path / "abc123" and d.is_dir()


def test_stable_json_sorted_keys():
    assert stable_json({"b": 1, "a": 2}) == stable_json({"a": 2, "b": 1})


def test_q2_rounds_half_up():
    # round(2.675, 2) == 2.67 — банковское округление, его тут быть не должно
    assert q2(Decimal("2.675")) == 2.68


def test_paths():
    assert WORK == ROOT / "work" and OUT == ROOT / "out"
