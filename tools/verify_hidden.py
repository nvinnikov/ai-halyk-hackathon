"""Проверка, что закрытый архив — тот самый, на котором считались все цифры разбора.

Архив лежит в репозитории целиком (публикация разрешена организаторами), и
именно поэтому сверка нужна: числа разбора привязаны к конкретным байтам, а
`dataset_hash` — первые шестнадцать знаков хеша архива — именует каталог
`work/<hash>/` со всеми артефактами прогона. Подменённый или пересобранный
архив тихо уводит прогон в другой каталог, и расхождение обнаружилось бы уже
по скору, а не по причине.

Хеши получены на архиве, по которому шёл боевой прогон 9 августа 2026.
"""

import hashlib
import sys
import zipfile
from pathlib import Path

ARCHIVE_SHA256 = "f1dc75c17f9a5e55925d016c363ea59305d23904895671ed37d9e455d4e811cd"
LEDGER_SHA256 = "7b91de638cf9de52e93990c8ee59eca0431c43f352fa350adaf72214bbb6b4f4"
TEMPLATE_SHA256 = "f5ac06838bae8fad11511435830410fb70b2fb379ed49ce8ecd69f5f9409039d"

# dataset_hash прогона — первые шестнадцать шестнадцатеричных знаков хеша
# архива; он же именует каталог work/<hash>/ и печатается первой строкой лога.
DATASET_HASH = ARCHIVE_SHA256[:16]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(path: Path) -> list[str]:
    """Список расхождений; пустой список — архив тот самый."""
    problems: list[str] = []
    got = _sha256(path.read_bytes())
    if got != ARCHIVE_SHA256:
        problems.append(f"архив целиком: {got} вместо {ARCHIVE_SHA256}")
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
        print("АРХИВ НЕ ТОТ:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"архив тот самый, dataset_hash: {DATASET_HASH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
