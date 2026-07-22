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
import yaml

# Физическая модель: как таблицы связаны между собой (знает движок, не LLM)
JOINS = {
    "deposits": "JOIN clients ON clients.client_id = deposits.client_id",
    "trades": "JOIN clients ON clients.client_id = trades.client_id",
    # marketing_spend/targets самодостаточны — джойн не нужен
}

# Дата-колонка каждой таблицы (для фильтра по году)
DATE_COLS = {
    "deposits": "deposits.deposit_date",
    "trades": "trades.trade_date",
    "marketing_spend": "marketing_spend.spend_date",
}


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

    # ---------- контекст для LLM ----------
    def catalog_for_llm(self) -> str:
        """Компактное описание метрик/измерений для промпта (LLM-формат)."""
        lines = ["METRICS:"]
        for name, m in self.metrics.items():
            syn = ", ".join(m.get("synonyms", []))
            lines.append(
                f"- {name}: {m['description'].strip()}\n"
                f"  synonyms: {syn}\n"
                f"  allowed_dimensions: {', '.join(m['allowed_dimensions'])}"
            )
        lines.append("\nDIMENSIONS:")
        for name, d in self.dimensions.items():
            syn = ", ".join(d.get("synonyms", []))
            sample = d.get("sample_values")
            extra = f" | sample: {sample}" if sample else ""
            lines.append(f"- {name} (synonyms: {syn}){extra}")
        return "\n".join(lines)

    def few_shot_for_llm(self) -> str:
        import json
        return "\n".join(
            f'Q: "{e["q"]}"\nA: {json.dumps(e["pick"], ensure_ascii=False)}'
            for e in self.examples
        )

    # ---------- валидация плана ----------
    def validate(self, plan: QueryPlan) -> list[str]:
        """Проверяем, что план ссылается только на сертифицированные объекты."""
        errors = []
        if plan.metric not in self.metrics:
            errors.append(f"Неизвестная метрика: '{plan.metric}'. "
                          f"Доступны: {', '.join(self.metrics)}")
            return errors  # дальше проверять нечего
        allowed = set(self.metrics[plan.metric]["allowed_dimensions"])
        for d in plan.group_by:
            if d not in self.dimensions:
                errors.append(f"Неизвестное измерение: '{d}'")
            elif d not in allowed:
                errors.append(
                    f"Метрику '{plan.metric}' нельзя резать по '{d}'. "
                    f"Разрешено: {', '.join(sorted(allowed))}")
        for f in plan.filters:
            if f not in self.dimensions and f != "year":
                errors.append(f"Неизвестное поле фильтра: '{f}'")
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
            f = ", ".join(f"{k}={v}" for k, v in plan.filters.items())
            parts.append(f"фильтр: {f}")
        if plan.limit:
            parts.append(f"топ-{plan.limit}")
        return "Я понял вопрос так: " + "; ".join(parts) + "."
