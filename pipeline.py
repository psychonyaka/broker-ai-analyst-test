"""
Оркестратор: вопрос на естественном языке -> ответ из данных.

Поток:
  вопрос
    -> LLM (structured output: JSON-план, НЕ SQL)
    -> валидация плана по semantic layer (allow-list метрик/измерений)
    -> детерминированная компиляция в SQL
    -> guardrails (SELECT-only, LIMIT, dry-run EXPLAIN)
    -> DuckDB
    -> ответ + "как я понял вопрос" (semantic transparency)

Запуск в CLI:  python pipeline.py "депозиты по странам"
"""
from dataclasses import dataclass
import os
import subprocess
import sys

import duckdb
import pandas as pd

import guardrails
from llm import build_prompt, get_provider
from semantic import QueryPlan, SemanticLayer

DB = "broker.duckdb"


@dataclass
class Answer:
    ok: bool
    question: str
    explanation: str = ""
    sql: str = ""
    data: pd.DataFrame | None = None
    error: str = ""
    provider: str = ""

    def __str__(self) -> str:
        if not self.ok:
            return f"[ОТКАЗ] {self.error}"
        head = f"{self.explanation}\n\nSQL:\n{self.sql}\n"
        return f"{head}\n{self.data.to_string(index=False)}"


class Chatbot:
    def __init__(self, db: str = DB, layer_path: str = "semantic_layer.yaml",
                 provider: str | None = None, allow_sql_fallback: bool = True):
        self.layer = SemanticLayer(layer_path)
        # Авто-генерация базы, если её нет (свежий clone / Streamlit Cloud):
        # проект должен подниматься одной командой, без ручного шага.
        if not os.path.exists(db):
            subprocess.run([sys.executable, "generate_data.py"], check=True)
        self.con = duckdb.connect(db, read_only=True)  # read-only = guardrail
        self.provider = get_provider(self.layer, provider)
        self.system = build_prompt(self.layer.catalog_for_llm(),
                                   self.layer.few_shot_for_llm())
        # 2-й уровень: governed SQL-fallback для вопросов вне semantic layer.
        # Требует настоящую LLM (fallback-провайдер SQL не пишет — и честно откажет).
        self.allow_sql_fallback = allow_sql_fallback

    def ask(self, question: str) -> Answer:
        a = Answer(ok=False, question=question, provider=self.provider.name)

        # 1) LLM -> структурированный план
        try:
            raw = self.provider.plan(question, self.system)
        except Exception as e:
            a.error = f"Ошибка LLM: {e}"
            return a

        if not raw.get("metric"):
            return self._sql_fallback(question, a, raw.get("reason"))

        plan = QueryPlan.from_dict(raw)

        # 2) валидация плана по semantic layer
        if errors := self.layer.validate(plan):
            a.error = "План не прошёл валидацию: " + "; ".join(errors)
            return a

        # 3) детерминированная компиляция + 4) guardrails + выполнение
        a.explanation = self.layer.explain(plan)
        try:
            a.sql = self.layer.compile_sql(plan)
            a.data = guardrails.safe_execute(self.con, a.sql)
        except guardrails.GuardrailError as e:
            a.error = f"Запрос отклонён guardrails: {e}"
            return a
        except Exception as e:
            a.error = f"Ошибка выполнения: {e}"
            return a

        a.ok = True
        return a

    def _sql_fallback(self, question: str, a: Answer, reason=None) -> Answer:
        """Уровень 2: вопрос вне semantic layer.

        Если провайдер умеет писать SQL (настоящая LLM) и fallback включён —
        генерируем ОДИН SELECT и прогоняем через ТЕ ЖЕ guardrails. Иначе —
        честный отказ с подсказкой, что система умеет.

        Ответ помечается как "вне сертифицированных метрик" — прозрачность:
        пользователь видит, что это ad-hoc, а не governed-метрика.
        """
        if not (self.allow_sql_fallback and hasattr(self.provider, "sql")):
            a.error = self.layer.reject_message(reason)
            return a

        try:
            sql = self.provider.sql(question)
        except Exception as e:
            a.error = self.layer.reject_message(f"SQL-fallback недоступен: {e}")
            return a

        try:
            a.sql = sql
            a.data = guardrails.safe_execute(self.con, sql)
        except guardrails.GuardrailError as e:
            a.error = f"Ad-hoc SQL отклонён guardrails: {e}"
            return a
        except Exception as e:
            a.error = self.layer.reject_message(f"не удалось выполнить SQL: {e}")
            return a

        a.explanation = ("⚠️ Вопрос вне сертифицированных метрик — ответ через "
                         "governed SQL-fallback (read-only + guardrails). "
                         "Это ad-hoc-запрос, а не сертифицированная метрика — "
                         "перепроверьте перед использованием в отчётности.")
        a.ok = True
        return a


def main():
    # Windows-консоль по умолчанию не UTF-8 — форсируем, чтобы не падать на кириллице
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    q = " ".join(sys.argv[1:]) or "депозиты по странам"
    bot = Chatbot()
    print(f"[provider: {bot.provider.name}]\n")
    print(f"ВОПРОС: {q}\n")
    print(bot.ask(q))


if __name__ == "__main__":
    main()
