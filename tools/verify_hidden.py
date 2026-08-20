"""Проверка, что данные закрытого набора в репозитории — те самые, на которых
считались все цифры разбора.

Набор хранится в git РАСПАКОВАННЫМ КАТАЛОГОМ (`dataset/agentic-bank-hidden/`),
как и публичный, — не переданным организаторами архивом, чтобы договоры можно
было открыть глазами, а не только распаковкой блоба. Архив, который принимает
`./run.sh`, собирается заново воспроизводимым упаковщиком
(`tools/hidden_archive.py`, `make hidden-archive`), а его байты — и с ними
`dataset_hash` (первые шестнадцать знаков sha256 архива, имя `work/<hash>/`) —
зависят от упаковщика, а не только от содержимого датасета. Сверять их с
байтами организаторской раздачи поэтому бессмысленно: другая реализация
упаковки дала бы другой хеш архива на тех же самых данных.

Сверяются файлы, чьи хеши от способа упаковки НЕ зависят: леджер и шаблон
ответа. Смысл проверки от перехода на каталог не потерялся, а стал строже:
она подтверждает, что данные в репозитории байт в байт те же, что раздали
организаторы и что опубликовала команда из топ-10, — просто не по хешу
архива целиком, а по хешам файлов внутри него.

Хеши получены на архиве, по которому шёл боевой прогон 9 августа 2026.
"""

import hashlib
import sys
import zipfile
from pathlib import Path

LEDGER_SHA256 = "7b91de638cf9de52e93990c8ee59eca0431c43f352fa350adaf72214bbb6b4f4"
TEMPLATE_SHA256 = "f5ac06838bae8fad11511435830410fb70b2fb379ed49ce8ecd69f5f9409039d"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(path: Path) -> list[str]:
    """Список расхождений; пустой список — данные внутри архива те самые."""
    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = {n.rsplit("/", 1)[-1]: n for n in z.namelist()}
        for label, filename, want in (
            ("леджер", "master_ledger_2025.csv", LEDGER_SHA256),
            ("шаблон ответа", "submission_template.json", TEMPLATE_SHA256),
        ):
            inner = names.get(filename)
            if inner is None:
                problems.append(f"{label}: файла {filename} в архиве нет")
                continue
            got_inner = _sha256(z.read(inner))
            if got_inner != want:
                problems.append(f"{label}: {got_inner} вместо {want}")
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print("использование: verify_hidden.py <архив.zip>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"файла нет: {path}", file=sys.stderr)
        return 2
    problems = verify(path)
    if problems:
        print("ДАННЫЕ НЕ ТЕ:")
        for p in problems:
            print(f"  {p}")
        return 1
    dataset_hash = _sha256(path.read_bytes())[:16]
    print(f"данные внутри архива те самые; dataset_hash этой сборки архива: {dataset_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
