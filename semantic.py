"""
Semantic engine: загружает semantic layer и ДЕТЕРМИНИРОВАННО компилирует
структурированный план (QueryPlan) в SQL.

Ключевая идея: LLM НЕ пишет SQL. Она возвращает только выбор
{metric, group_by, filters, order, limit} из сертифицированных блоков,
а корректный SQL (джойны, грейн, built-in фильтры) собирает этот модуль.
Это резко сокращает пространство ошибок модели.
"""
from dataclasses import dataclass, field
from typing import Any
import re
import yaml

# Вопросы «о метрике», а не «по данным»: на них отвечаем из слоя, без SQL
DEFINITION_RE = re.compile(
    r"что такое|что значит|что означает|как счита|как определ|определени|"
    r"как понима|что входит в", re.IGNORECASE)


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def _word_match(a: str, b: str) -> bool:
    """Совпадение по общей основе (учет падежей)."""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 4 and n >= min(len(a), len(b)) - 2


def _phrase_in(phrase: str, words: list[str]) -> bool:
    """Все значимые слова фразы присутствуют в вопросе."""
    toks = [t for t in re.findall(r"\w+", _norm(phrase)) if len(t) >= 4]
    return bool(toks) and all(any(_word_match(t, w) for w in words) for t in toks)

# Физическая модель: как таблицы связаны между собой (знает движок, не LLM)
JOINS = {
    "deposits": "JOIN clients ON clients.client_id = deposits.client_id",
    "trades": "JOIN clients ON clients.client_id = trades.client_id",
    # marketing_spend/targets самодостаточны — джойн не нужен
}

# Дата-колонка каждой таблицы (для фильтров по времени)
DATE_COLS = {
    "deposits": "deposits.deposit_date",
    "trades": "trades.trade_date",
    "marketing_spend": "marketing_spend.spend_date",
    "clients": "clients.registration_date",
}

# Относительные периоды: «за прошлый месяц», «последний квартал», «с начала года».
# Считаются от CURRENT_DATE — движком, а не LLM (та лишь выбирает ярлык периода).
PERIODS = {
    "last_month":   "{d} >= date_trunc('month', CURRENT_DATE - INTERVAL 1 MONTH) "
                    "AND {d} < date_trunc('month', CURRENT_DATE)",
    "this_month":   "{d} >= date_trunc('month', CURRENT_DATE)",
    "last_quarter": "{d} >= date_trunc('quarter', CURRENT_DATE - INTERVAL 3 MONTH) "
                    "AND {d} < date_trunc('quarter', CURRENT_DATE)",
    "this_quarter": "{d} >= date_trunc('quarter', CURRENT_DATE)",
    "last_year":    "{d} >= date_trunc('year', CURRENT_DATE - INTERVAL 1 YEAR) "
                    "AND {d} < date_trunc('year', CURRENT_DATE)",
    "ytd":          "{d} >= date_trunc('year', CURRENT_DATE)",
}

# Служебные ключи фильтров (не измерения)
SPECIAL_FILTERS = {"year", "period", "last_n_months"}


@dataclass
class QueryPlan:
    """Структурированный выбор — то, что возвращает LLM."""
    metric: str
    group_by: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    order: str = "desc"
    limit: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "QueryPlan":
        return cls(
            metric=d.get("metric", ""),
            group_by=d.get("group_by") or [],
            filters=d.get("filters") or {},
            order=d.get("order", "desc"),
            limit=d.get("limit"),
        )


class SemanticLayer:
    def __init__(self, path: str = "semantic_layer.yaml"):
        with open(path, encoding="utf-8") as f:
            self.spec = yaml.safe_load(f)
        self.metrics: dict = self.spec["metrics"]
        self.dimensions: dict = self.spec["dimensions"]
        self.examples: list = self.spec.get("examples", [])
        self.roles: dict = self.spec.get("roles", {})

    # ---------- ролевой доступ (RLS-lite) ----------
    def metrics_for_role(self, role: str | None) -> set:
        """Множество метрик, доступных роли. None или неизвестная роль -> все."""
        spec = self.roles.get(role or "")
        if not spec or spec.get("metrics") == "*":
            return set(self.metrics)
        return set(spec["metrics"]) & set(self.metrics)

    def forbidden_tokens_for_role(self, role: str | None) -> set:
        """Колонки/таблицы скрытых метрик — их нельзя доставать даже ad-hoc SQL (L2).

        Иначе роль обошла бы ограничение, запросив «сырую» колонку напрямую."""
        allowed = self.metrics_for_role(role)
        hidden = set(self.metrics) - allowed
        if not hidden:
            return set()
        toks = set()
        for name in hidden:
            expr = self.metrics[name].get("expression", "").lower()
            toks |= set(re.findall(r"\b\w+_usd\b", expr))   # напр. pnl_usd, cost_usd
            toks.add(self.metrics[name].get("table", "").lower())
        # не запрещаем то, что нужно РАЗРЕШЕННЫМ метрикам
        allowed_tables = {self.metrics[n].get("table", "").lower() for n in allowed}
        return {t for t in toks if t and t not in allowed_tables}

    # ---------- контекст для LLM ----------
    def catalog_for_llm(self, role: str | None = None) -> str:
        """Компактное описание метрик/измерений для промпта (LLM-формат).

        role — если задана, в каталог попадают только разрешенные метрики
        (модель не узнает о существовании скрытых)."""
        allowed = self.metrics_for_role(role)
        lines = []
        # Глоссарий бизнес-правил: модель не догадается о них сама
        if ctx := self.spec.get("business_context"):
            lines += ["BUSINESS CONTEXT (обязательно учитывать):",
                      ctx.strip(), ""]
        lines.append("METRICS:")
        for name, m in self.metrics.items():
            if name not in allowed:
                continue
            syn = ", ".join(m.get("synonyms", []))
            block = (f"- {name}: {m['description'].strip()}\n"
                     f"  synonyms: {syn}\n"
                     f"  allowed_dimensions: {', '.join(m['allowed_dimensions'])}")
            # примеры вопросов, на которые отвечает метрика (Lightdash-практика)
            if ex := m.get("example_questions"):
                block += "\n  answers questions like: " + "; ".join(f'"{q}"' for q in ex)
            lines.append(block)
        lines.append("\nDIMENSIONS:")
        for name, d in self.dimensions.items():
            syn = ", ".join(d.get("synonyms", []))
            lines.append(f"- {name} (synonyms: {syn})")
            if desc := d.get("description"):
                lines.append(f"  {desc.strip()}")
            if sample := d.get("sample_values"):
                # ПОЛНЫЙ список значений — чтобы LLM корректно строила фильтры
                lines.append(f"  values: {sample}")
        return "\n".join(lines)

    def few_shot_for_llm(self) -> str:
        import json
        return "\n".join(
            f'Q: "{e["q"]}"\nA: {json.dumps(e["pick"], ensure_ascii=False)}'
            for e in self.examples
        )

    # ---------- валидация плана ----------
    def validate(self, plan: QueryPlan, role: str | None = None) -> list[str]:
        """Проверяем, что план ссылается только на сертифицированные объекты."""
        errors = []
        if plan.metric not in self.metrics:
            errors.append(f"Неизвестная метрика: '{plan.metric}'. "
                          f"Доступны: {', '.join(self.metrics)}")
            return errors  # дальше проверять нечего
        # ролевой доступ: метрика есть, но роли не разрешена
        if plan.metric not in self.metrics_for_role(role):
            errors.append(f"Метрика '{plan.metric}' недоступна для вашей роли.")
            return errors
        allowed = set(self.metrics[plan.metric]["allowed_dimensions"])
        for d in plan.group_by:
            if d not in self.dimensions:
                errors.append(f"Неизвестное измерение: '{d}'")
            elif d not in allowed:
                errors.append(
                    f"Метрику '{plan.metric}' нельзя резать по '{d}'. "
                    f"Разрешено: {', '.join(sorted(allowed))}")
        for f in plan.filters:
            if f not in self.dimensions and f not in SPECIAL_FILTERS:
                errors.append(f"Неизвестное поле фильтра: '{f}'")
        if (p := plan.filters.get("period")) and p not in PERIODS:
            errors.append(f"Неизвестный период: '{p}'. Доступны: {', '.join(PERIODS)}")
        return errors

    # ---------- компиляция в SQL ----------
    def compile_sql(self, plan: QueryPlan) -> str:
        m = self.metrics[plan.metric]
        base = m["table"]

        select_parts, group_parts = [], []
        for d in plan.group_by:
            select_parts.append(f'{self.dimensions[d]["sql"]} AS {d}')
            group_parts.append(self.dimensions[d]["sql"])
        select_parts.append(f'{m["expression"]} AS {plan.metric}')

        # WHERE: built-in фильтры метрики (всегда) + пользовательские
        where = []
        if m.get("filters_builtin"):
            where.append(f'({m["filters_builtin"]})')
        for field_name, value in plan.filters.items():
            if field_name == "year":
                date_col = DATE_COLS.get(base, f"{base}.date")
                where.append(f"EXTRACT(year FROM {date_col}) = {int(value)}")
            elif field_name == "period":
                date_col = DATE_COLS.get(base, f"{base}.date")
                where.append("(" + PERIODS[value].format(d=date_col) + ")")
            elif field_name == "last_n_months":
                date_col = DATE_COLS.get(base, f"{base}.date")
                where.append(
                    f"{date_col} >= date_trunc('month', CURRENT_DATE - "
                    f"INTERVAL {int(value)} MONTH)")
            else:
                col = self.dimensions[field_name]["sql"]
                if isinstance(value, (list, tuple)):
                    vals = ", ".join(f"'{str(v)}'" for v in value)
                    where.append(f"{col} IN ({vals})")
                else:
                    safe = str(value).replace("'", "''")
                    where.append(f"{col} = '{safe}'")

        sql = f"SELECT {', '.join(select_parts)}\nFROM {base}"
        if JOINS.get(base):
            sql += f"\n{JOINS[base]}"
        if where:
            sql += "\nWHERE " + " AND ".join(where)
        if group_parts:
            sql += "\nGROUP BY " + ", ".join(group_parts)
            direction = "DESC" if plan.order == "desc" else "ASC"
            # по временным измерениям сортируем хронологически
            if any(self.dimensions[d].get("grain") == "month" for d in plan.group_by):
                sql += f"\nORDER BY 1 ASC"
            else:
                sql += f"\nORDER BY {plan.metric} {direction}"
        if plan.limit:
            sql += f"\nLIMIT {int(plan.limit)}"
        return sql

    # ---------- вопросы О МЕТРИКЕ (без SQL) ----------
    def definition_answer(self, question: str, role: str | None = None) -> str | None:
        """«Что такое активный трейдер?» -> объяснение из контракта метрики.

        Семантический слой — это еще и документация: определение, встроенные
        фильтры, формула и владелец лежат рядом. Отвечаем прямо из слоя,
        без обращения к БД (данных такой вопрос не требует).
        Скрытые для роли метрики не раскрываются даже как определение.
        """
        if not DEFINITION_RE.search(question):
            return None
        allowed = self.metrics_for_role(role)
        words = re.findall(r"\w+", _norm(question))
        best, best_len = None, 0
        for name, m in self.metrics.items():
            if name not in allowed:
                continue
            for token in [name, m.get("label", ""), *m.get("synonyms", [])]:
                if token and _phrase_in(token, words) and len(token) > best_len:
                    best, best_len = name, len(token)
        if not best:
            return None

        m = self.metrics[best]
        out = [f"**{m['label']}** (`{best}`)", "", m["description"].strip(), "",
               f"Формула: `{m['expression']}`"]
        if f := m.get("filters_builtin"):
            out.append(f"Всегда применяется фильтр: `{f}`")
        out += [f"Разрешенные разрезы: {', '.join(m['allowed_dimensions'])}",
                f"Владелец метрики: {m.get('owner', '—')}"]
        if ex := m.get("example_questions"):
            out += ["", "Примеры вопросов: " + "; ".join(f'«{q}»' for q in ex)]
        return "\n".join(out)

    # ---------- дружелюбный отказ ----------
    def reject_message(self, reason: str | None = None) -> str:
        """Понятный отказ: не просто «не смог», а что система УМЕЕТ.

        Governance-фича: система честно отказывается вместо галлюцинации,
        но помогает пользователю переформулировать в рамках сертифицированных метрик.
        """
        labels = "\n".join(f"  • {m['label']}" for m in self.metrics.values())
        examples = [e["q"] for e in self.examples[:3]]
        ex = "; ".join(f'«{q}»' for q in examples) if examples else ""
        msg = ["Не смог сопоставить вопрос с сертифицированными метриками — "
               "отвечаю только по проверенным показателям, чтобы не выдумывать цифры.",
               "", "Я умею считать:", labels]
        if ex:
            msg += ["", f"Попробуйте, например: {ex}"]
        if reason:
            msg += ["", f"(детали: {reason})"]
        return "\n".join(msg)

    # ---------- человекочитаемое "как я понял вопрос" ----------
    def explain(self, plan: QueryPlan) -> str:
        m = self.metrics[plan.metric]
        parts = [f"метрика **{m['label']}**"]
        if plan.group_by:
            parts.append("разрез по **" + "**, **".join(plan.group_by) + "**")
        if plan.filters:
            human = {"last_month": "прошлый месяц", "this_month": "текущий месяц",
                     "last_quarter": "прошлый квартал", "this_quarter": "текущий квартал",
                     "last_year": "прошлый год", "ytd": "с начала года"}
            bits = []
            for k, v in plan.filters.items():
                if k == "period":
                    bits.append(f"период: {human.get(v, v)}")
                elif k == "last_n_months":
                    bits.append(f"последние {v} мес.")
                else:
                    bits.append(f"{k}={v}")
            parts.append("фильтр: " + ", ".join(bits))
        if plan.limit:
            parts.append(f"топ-{plan.limit}")
        return "Я понял вопрос так: " + "; ".join(parts) + "."
