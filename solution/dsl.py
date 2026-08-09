"""DSL метрик (5.4): маленький и тотальный, парсится грамматикой до исполнения.

expr    := agg(category, sign, filters?) | doc(key) | ratio(a,b) | sub(a,b)
         | add(a...) | max(a...) | min(a...) | const(x) | mul(a,b)
trigger := gt(a,b) | ge(a,b) | lt(a,b) | le(a,b)
filters := period(from,to) | quarter(n) | counterparty_in(set) | txn_in(ids)
         | min_amount(x) | desc_contains(s)
set     := related_parties | unrestricted_subsidiaries | ['литерал', ...]
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from engine import LEGAL_FORMS
from taxonomy import is_category


class DslError(Exception):
    """Выражение не по грамматике — фолбэк, не исполнение."""


@dataclass(frozen=True)
class Period:
    frm: str
    to: str


@dataclass(frozen=True)
class Quarter:
    n: int


@dataclass(frozen=True)
class CounterpartyIn:
    setname: object  # 'related_parties' | 'unrestricted_subsidiaries' | tuple[str, ...]


@dataclass(frozen=True)
class TxnIn:
    ids: tuple


@dataclass(frozen=True)
class MinAmount:
    x: Decimal


@dataclass(frozen=True)
class DescContains:
    s: str


@dataclass(frozen=True)
class Agg:
    category: str
    sign: str
    filters: tuple = ()


@dataclass(frozen=True)
class Doc:
    key: str


@dataclass(frozen=True)
class Ratio:
    num: object
    den: object


@dataclass(frozen=True)
class Sub:
    a: object
    b: object


@dataclass(frozen=True)
class Add:
    args: tuple


@dataclass(frozen=True)
class MaxOf:
    args: tuple


@dataclass(frozen=True)
class MinOf:
    args: tuple


@dataclass(frozen=True)
class Const:
    value: Decimal


@dataclass(frozen=True)
class Mul:
    """Произведение с константным множителем — процентные кэпы договоров
    («добавки не более такой-то доли выручки»). Хотя бы один аргумент обязан
    быть Const: произведение двух агрегатов ковенантной семантики не имеет,
    а тихо разрешённое дало бы класс правдоподобно-огромных чисел."""

    a: object
    b: object


@dataclass(frozen=True)
class Cmp:
    op: str
    a: object
    b: object


_FILTER_TYPES = (Period, Quarter, CounterpartyIn, TxnIn, MinAmount, DescContains)

_TOKEN = re.compile(
    r"\s*(?:(?P<lpar>\()|(?P<rpar>\))|(?P<lbr>\[)|(?P<rbr>\])|(?P<comma>,)|(?P<eq>=)"
    r"|(?P<str>'[^']*')|(?P<date>\d{4}-\d{2}-\d{2})|(?P<num>-?\d+(?:\.\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*))"
)

_SIGNS = {"out", "in", "net"}
_SETS = {"related_parties", "unrestricted_subsidiaries"}
_FILTERS = {"period", "quarter", "counterparty_in", "txn_in", "min_amount", "desc_contains"}

# Основы слов, по которым строка распознаётся как НАЗВАНИЕ множества, а не имя
# контрагента. Не привязка к языку договора: обе формулировки, которыми
# множество называют в тексте, — на равных правах, набор расширяется, а не
# заменяется. Основы, не полные фразы: падежи и род («связанных сторон»,
# «аффилированного лица») ловятся тем же элементом.
_SET_STEMS = {
    "related_parties": ("related part", "affiliat", "связанн", "аффилир"),
    "unrestricted_subsidiaries": ("unrestricted sub", "необремен", "неограничен"),
}


def _as_set_name(value: object) -> str | None:
    """Имя множества, если строка — его название; иначе None.

    Литеральный список у counterparty_in — перечень контрагентов, и матч в
    интерпретаторе идёт по токенам имени. Название множества человеческими
    словами (`['аффилированные лица']`) не совпадёт ни с одним контрагентом:
    фильтр отсечёт весь леджер и ячейка гарантированно обнулится. Поэтому такая
    строка разрешается в само множество — форма аргумента, а не данные.
    """
    if not isinstance(value, str):
        return None
    if value in _SETS:
        return value
    norm = " ".join(value.lower().replace("ё", "е").split())
    # Признак юрлица закрывает разрешение: основы названий множеств («affiliat»,
    # «связанн») встречаются и в настоящих именах компаний, а название множества
    # юрформы не содержит. Промах в эту сторону молчалив и дорог — вместо одного
    # контрагента фильтр берёт весь набор связанных сторон, сумма растёт на
    # чужие строки, вердикт остаётся правдоподобным и алярма нет. Обратный
    # промах даёт нулевую агрегацию и ловится лестницей фолбэков.
    if LEGAL_FORMS & set(re.split(r"[^\w]+", norm)):
        return None
    for setname, stems in sorted(_SET_STEMS.items()):
        if any(stem in norm for stem in stems):
            return setname
    return None


def _tokenize(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        kind = m.lastgroup if m else None
        if not m or m.end() == m.start() or kind is None:
            raise DslError(f"мусор в выражении на позиции {pos}: {text[pos : pos + 10]!r}")
        pos = m.end()
        out.append((kind, m.group(kind)))
    return out


class _Parser:
    """Рекурсивный спуск. Фильтры разбираются только в хвосте agg(...):
    вне него period/quarter/... — это DslError (тест закрепляет)."""

    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, kind):
        k, v = self.peek()
        if k != kind:
            raise DslError(f"ожидался {kind}, встретился {k}:{v!r}")
        self.i += 1
        return v

    def parse_call(self, allow_filter: bool = False):
        name = self.take("name")
        self.take("lpar")
        args = []
        if self.peek()[0] != "rpar":
            args.append(self.parse_arg(in_agg=(name == "agg"), pos=0))
            while self.peek()[0] == "comma":
                self.take("comma")
                args.append(self.parse_arg(in_agg=(name == "agg"), pos=len(args)))
        self.take("rpar")
        if name in _FILTERS:
            if not allow_filter:
                raise DslError(f"фильтр {name} вне agg")
            return _build_filter(name, args)
        return _build_node(name, args)

    def parse_arg(self, in_agg: bool, pos: int):
        k, v = self.peek()
        if (
            k == "name"
            and v == "filters"
            and self.i + 1 < len(self.toks)
            and self.toks[self.i + 1][0] == "eq"
        ):
            # Модель эхом печатает имя поля AST: agg(..., filters=[f1, f2])
            # вместо голого хвоста фильтров (живой паттерн Gemini, task-28).
            # Список после filters= — тот же хвост, легален там же, где
            # легален фильтр: только в хвосте agg (позиция ≥ 2).
            if not (in_agg and pos >= 2):
                raise DslError("filters=[...] вне хвоста agg")
            self.take("name")
            self.take("eq")
            self.take("lbr")
            # filters=[] — пустое эхо поля, равное отсутствию хвоста
            # (ревью PR #11, раунд 2).
            items = []
            if self.peek()[0] != "rbr":
                items.append(self.parse_call(allow_filter=True))
            while self.peek()[0] == "comma":
                self.take("comma")
                items.append(self.parse_call(allow_filter=True))
            self.take("rbr")
            return ("filters_list", tuple(items))
        if k == "name" and self.i + 1 < len(self.toks) and self.toks[self.i + 1][0] == "lpar":
            # вложенный вызов; фильтр легален только в хвосте agg (позиция ≥ 2)
            return self.parse_call(allow_filter=in_agg and pos >= 2)
        if k in ("name", "date"):
            self.i += 1
            return (k, v)
        if k == "num":
            self.i += 1
            return ("num", Decimal(v))
        if k == "str":
            self.i += 1
            return ("str", v[1:-1])
        if k == "lbr":
            self.take("lbr")
            # Голый список фильтров — agg(ALL, out, [period(...), ...]) — та же
            # путаница форм, что filters=[...], только без имени поля (живой
            # паттерн боевого прогона: три ячейки одного заёмщика). Отличается
            # от списка контрагентов первым же токеном: фильтр — это вызов
            # name(, строка-литерал — нет. Легален там же, где легален фильтр.
            if (
                self.peek()[0] == "name"
                and self.i + 1 < len(self.toks)
                and self.toks[self.i + 1][0] == "lpar"
            ):
                if not (in_agg and pos >= 2):
                    raise DslError("[...] с фильтрами вне хвоста agg")
                items_f = [self.parse_call(allow_filter=True)]
                while self.peek()[0] == "comma":
                    self.take("comma")
                    items_f.append(self.parse_call(allow_filter=True))
                self.take("rbr")
                return ("filters_list", tuple(items_f))
            items = [self.take("str")[1:-1]]
            while self.peek()[0] == "comma":
                self.take("comma")
                items.append(self.take("str")[1:-1])
            self.take("rbr")
            return ("list", tuple(items))
        raise DslError(f"неожиданный токен {k}:{v!r}")


_LITERAL_KINDS = ("name", "num", "date", "str", "list")


def _is_lit(x, *kinds):
    return isinstance(x, tuple) and len(x) == 2 and x[0] in kinds


def _expr(x):
    if _is_lit(x, *_LITERAL_KINDS):
        raise DslError(f"ожидалось выражение, встретился литерал {x!r}")
    if isinstance(x, _FILTER_TYPES):
        raise DslError(f"фильтр {x!r} на месте выражения")
    return x


_DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NAME_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUM_SHAPE = re.compile(r"-?\d+(?:\.\d+)?")  # та же форма, что у токена num


def _lit(x, *kinds):
    if not _is_lit(x, *kinds) and _is_lit(x, "str"):
        # Та же путаница форм, что у counterparty_in('related_parties'):
        # модель кавычит то, что грамматика ждёт голым литералом —
        # period('2025-01-01', ...) и doc('ключ') с живых прогонов Gemini
        # (task-28, «третий паттерн»). Строка, чьё содержимое имеет форму
        # ожидаемого литерала, — тот же литерал; любая другая — ошибка.
        s = x[1]
        if "date" in kinds and _DATE_SHAPE.fullmatch(s):
            return s
        if "name" in kinds and _NAME_SHAPE.fullmatch(s):
            return s
        # Симметрия для чисел (ревью PR #11): quarter('1'), min_amount('…'),
        # const('…'). Тип — Decimal, как у голой num-формы из parse_arg,
        # иначе Const/MinAmount разъедутся по типу поля и сломают signature().
        if "num" in kinds and _NUM_SHAPE.fullmatch(s):
            return Decimal(s)
    if not _is_lit(x, *kinds):
        raise DslError(f"ожидался литерал {kinds}, встретился {x!r}")
    return x[1]


def _build_node(name, args):
    if name == "agg":
        if len(args) < 2:
            raise DslError("agg(category, sign, filters?)")
        sign = _lit(args[1], "name")
        if sign not in _SIGNS:
            raise DslError(f"sign {sign!r} не из {sorted(_SIGNS)}")
        filters = []
        for a in args[2:]:
            if _is_lit(a, "filters_list"):
                for f in a[1]:
                    if not isinstance(f, _FILTER_TYPES):
                        raise DslError(f"в filters=[...] ожидался фильтр, встретился {f!r}")
                    filters.append(f)
                continue
            if not isinstance(a, _FILTER_TYPES):
                raise DslError(f"в хвосте agg ожидался фильтр, встретился {a!r}")
            filters.append(a)
        return Agg(category=_lit(args[0], "name"), sign=sign, filters=tuple(filters))
    if name == "doc" and len(args) == 1:
        return Doc(key=_lit(args[0], "name"))
    if name == "ratio" and len(args) == 2:
        return Ratio(num=_expr(args[0]), den=_expr(args[1]))
    if name == "sub" and len(args) == 2:
        return Sub(a=_expr(args[0]), b=_expr(args[1]))
    if name == "add" and args:
        return Add(args=tuple(_expr(a) for a in args))
    if name == "max" and args:
        return MaxOf(args=tuple(_expr(a) for a in args))
    if name == "min" and args:
        return MinOf(args=tuple(_expr(a) for a in args))
    if name == "const" and len(args) == 1:
        return Const(value=_lit(args[0], "num"))
    if name == "mul" and len(args) == 2:
        # Терпимость форм по образцу const(): числовой множитель, написанный
        # голым числом или кавыченной строкой-числом — mul(x, agg(...)) —
        # промотируется в Const тем же _lit-механизмом. Любая другая строка —
        # ошибка, как везде.
        operands = [Const(value=_lit(a, "num")) if _is_lit(a, "num", "str") else _expr(a) for a in args]
        if not any(isinstance(o, Const) for o in operands):
            raise DslError("mul: хотя бы один аргумент — константа")
        return Mul(a=operands[0], b=operands[1])
    if name in ("gt", "ge", "lt", "le") and len(args) == 2:
        return Cmp(op=name, a=_expr(args[0]), b=_expr(args[1]))
    raise DslError(f"неизвестная конструкция {name}/{len(args)}")


def _build_filter(name, args):
    if name == "period" and len(args) == 2:
        return Period(frm=_lit(args[0], "date"), to=_lit(args[1], "date"))
    if name == "quarter" and len(args) == 1:
        return Quarter(n=int(_lit(args[0], "num")))
    if name == "counterparty_in" and len(args) == 1:
        if _is_lit(args[0], "name"):
            setname = args[0][1]
            if setname not in _SETS:
                raise DslError(f"неизвестное множество {setname!r}")
            return CounterpartyIn(setname=setname)
        if _is_lit(args[0], "list"):
            items = args[0][1]
            # Список, целиком являющийся названием одного множества, — то же
            # множество: `['аффилированные лица']` перечисляет не контрагентов.
            # Целиком, а не по одному элементу: смешанный список (описание плюс
            # настоящее имя) остаётся буквальным — там имя ещё сматчится, а
            # подмена множеством стёрла бы его.
            resolved = {_as_set_name(i) for i in items} if items else {None}
            if len(resolved) == 1 and (setname := resolved.pop()) is not None:
                return CounterpartyIn(setname=setname)
            return CounterpartyIn(setname=items)
        if _is_lit(args[0], "str"):
            # Модель иногда путает две формы аргумента и кавычит то, что
            # грамматика ждёт голым идентификатором: counterparty_in
            # ('related_parties') вместо counterparty_in(related_parties).
            # Строка, называющая известное множество, — то же множество; любая
            # другая строка — список из одного контрагента.
            value = args[0][1]
            return CounterpartyIn(setname=_as_set_name(value) or (value,))
    if name == "txn_in" and len(args) == 1 and _is_lit(args[0], "list"):
        return TxnIn(ids=args[0][1])
    if name == "min_amount" and len(args) == 1:
        return MinAmount(x=_lit(args[0], "num"))
    if name == "desc_contains" and len(args) == 1:
        return DescContains(s=_lit(args[0], "str"))
    raise DslError(f"неизвестный фильтр {name}/{len(args)}")


def parse(text: str):
    # LLM почти всегда отдаёт хвостовой перевод строки — это не мусор
    toks = _tokenize(text.strip())
    if not toks:
        raise DslError("пустое выражение")
    p = _Parser(toks)
    node = p.parse_call()
    if p.i != len(toks):
        raise DslError(f"лишние токены после выражения: {p.toks[p.i :]}")
    if isinstance(node, _FILTER_TYPES):
        raise DslError("фильтр вне agg")
    return node


def walk(node):
    yield node
    for child in getattr(node, "__dict__", {}).values():
        if isinstance(child, tuple):
            for c in child:
                if hasattr(c, "__dataclass_fields__"):
                    yield from walk(c)
        elif hasattr(child, "__dataclass_fields__"):
            yield from walk(child)


def validate(node, fact_keys: set[str]) -> list[str]:
    errors = []
    for n in walk(node):
        if isinstance(n, Agg) and not is_category(n.category):
            errors.append(f"категория {n.category!r} вне таксономии")
        if isinstance(n, Doc) and n.key not in fact_keys:
            errors.append(f"doc-ключ {n.key!r} отсутствует в досье")
    return errors


def signature(node) -> str:
    """Каноническая форма с затёртыми константами и знаком — для матча с шаблонами.

    Знак затирается, чтобы извлечённая спека с net матчилась с out-шаблоном;
    если после этого две разные метрики слиплись — конфликт решать по категории,
    не откатывать затирание (правило из задачи 15)."""
    if isinstance(node, Const):
        return "const(#)"
    if isinstance(node, MinAmount):
        return "min_amount(#)"
    if not hasattr(node, "__dataclass_fields__"):
        return repr(node)
    parts = []
    for k in sorted(node.__dataclass_fields__):
        v = getattr(node, k)
        if isinstance(node, Agg) and k == "sign":
            v = "#"
        if isinstance(v, tuple):
            parts.append(
                "["
                + ",".join(signature(c) if hasattr(c, "__dataclass_fields__") else repr(c) for c in v)
                + "]"
            )
        elif hasattr(v, "__dataclass_fields__"):
            parts.append(signature(v))
        else:
            parts.append(repr(v))
    return f"{type(node).__name__}({','.join(parts)})"


def uses_ledger(node) -> bool:
    return any(isinstance(n, Agg) for n in walk(node))


def unparse(node) -> str:
    """Текст по AST, обратный parse: parse(unparse(x)) == x.

    Потребитель — нормализация EBITDA-подвыражений по определению из договора
    (solve._apply_ebitda_reading): cellspec несёт и AST, и текст (текст читают
    трейс и теневое сравнение), поэтому переписанный AST обязан уметь
    вернуться в текст той же грамматики. Функция уже жила в ветке PR #26 для
    переноса фильтров и была откачена вместе с ним как код без потребителя;
    возвращена дословно."""
    if isinstance(node, Agg):
        tail = "".join(f", {unparse(f)}" for f in node.filters)
        return f"agg({node.category}, {node.sign}{tail})"
    if isinstance(node, Period):
        return f"period({node.frm}, {node.to})"
    if isinstance(node, Quarter):
        return f"quarter({node.n})"
    if isinstance(node, CounterpartyIn):
        if isinstance(node.setname, str):
            return f"counterparty_in({node.setname})"
        # setname объявлен object (str | tuple); не-строка — только кортеж имён
        names = node.setname if isinstance(node.setname, tuple) else ()
        items = ", ".join(f"'{i}'" for i in names)
        return f"counterparty_in([{items}])"
    if isinstance(node, TxnIn):
        items = ", ".join(f"'{i}'" for i in node.ids)
        return f"txn_in([{items}])"
    if isinstance(node, MinAmount):
        return f"min_amount({node.x})"
    if isinstance(node, DescContains):
        return f"desc_contains('{node.s}')"
    if isinstance(node, Doc):
        return f"doc({node.key})"
    if isinstance(node, Ratio):
        return f"ratio({unparse(node.num)}, {unparse(node.den)})"
    if isinstance(node, Sub):
        return f"sub({unparse(node.a)}, {unparse(node.b)})"
    if isinstance(node, Add):
        return f"add({', '.join(unparse(a) for a in node.args)})"
    if isinstance(node, MaxOf):
        return f"max({', '.join(unparse(a) for a in node.args)})"
    if isinstance(node, MinOf):
        return f"min({', '.join(unparse(a) for a in node.args)})"
    if isinstance(node, Const):
        return f"const({node.value})"
    if isinstance(node, Mul):
        return f"mul({unparse(node.a)}, {unparse(node.b)})"
    if isinstance(node, Cmp):
        return f"{node.op}({unparse(node.a)}, {unparse(node.b)})"
    raise TypeError(f"не узел DSL: {node!r}")
