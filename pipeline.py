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
import json
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
    context: dict | None = None  # для multi-turn: план/SQL этого ответа
    retried: bool = False        # ответ получен со второй попытки (self-correction)

    def __str__(self) -> str:
        if not self.ok:
            return f"[ОТКАЗ] {self.error}"
        if self.data is None:          # ответ-определение: данных нет
            return self.explanation
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

    def ask(self, question: str, prev_context: dict | None = None) -> Answer:
        """prev_context — контекст предыдущего ответа (для multi-turn follow-up).

        Передаётся явно (не глобально), чтобы eval/CLI оставались single-turn,
        а Streamlit хранил контекст в рамках своей сессии.
        """
        a = Answer(ok=False, question=question, provider=self.provider.name)

        # 0) вопрос О МЕТРИКЕ («что такое активный трейдер?») — отвечаем из
        #    семантического слоя, без обращения к БД
        if definition := self.layer.definition_answer(question):
            a.ok, a.explanation = True, definition
            return a

        # 1) LLM -> структурированный план (с контекстом предыдущего вопроса)
        try:
            raw = self.provider.plan(self._augment_plan(question, prev_context),
                                     self.system)
        except Exception as e:
            a.error = f"Ошибка LLM: {e}"
            return a

        if not raw.get("metric"):
            return self._sql_fallback(question, a, raw.get("reason"), prev_context)

        plan = QueryPlan.from_dict(raw)

        # 2) валидация плана по semantic layer (+ одна попытка самокоррекции)
        if errors := self.layer.validate(plan):
            if fixed := self._retry_plan(question, prev_context, errors):
                plan, a.retried = fixed, True
                errors = self.layer.validate(plan)
            if errors:
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

        a.context = {"question": question, "sql": a.sql,
                     "plan": {"metric": plan.metric, "group_by": plan.group_by,
                              "filters": plan.filters, "order": plan.order,
                              "limit": plan.limit}}

        a.ok = True
        return a

    def _can_retry(self) -> bool:
        """Самокоррекция имеет смысл только с настоящей LLM.

        Fallback-провайдер детерминирован — повтор вернёт тот же результат."""
        return not str(self.provider.name).startswith("fallback")

    def _retry_plan(self, question: str, prev: dict | None,
                    errors: list[str]) -> QueryPlan | None:
        """Одна попытка: показываем модели ошибку валидации, просим исправить.

        Ретраим ТОЛЬКО технический сбой (недопустимый разрез/метрика).
        Осознанный отказ (metric=null) не ретраится — иначе система начнёт
        «уговаривать себя» ответить на то, на что отвечать не должна.
        """
        if not self._can_retry():
            return None
        note = ("\n\n--- ТВОЙ ПРЕДЫДУЩИЙ ПЛАН НЕ ПРОШЁЛ ВАЛИДАЦИЮ ---\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\nВерни ИСПРАВЛЕННЫЙ план строго из каталога. "
                  "Если корректного варианта нет — верни {\"metric\": null}.")
        try:
            raw = self.provider.plan(self._augment_plan(question, prev) + note,
                                     self.system)
        except Exception:
            return None
        return QueryPlan.from_dict(raw) if raw.get("metric") else None

    def _retry_sql(self, question: str, bad_sql: str, err: str) -> str | None:
        """Одна попытка: отдаём модели текст ошибки БД/guardrails, просим починить."""
        if not self._can_retry():
            return None
        note = (f"{question}\n\n--- ТВОЙ ПРЕДЫДУЩИЙ SQL УПАЛ ---\n{bad_sql}\n"
                f"Ошибка: {err}\nВерни ИСПРАВЛЕННЫЙ SQL (или NO_DATA).")
        try:
            return self.provider.sql(note)
        except Exception:
            return None

    def _augment_plan(self, question: str, prev: dict | None) -> str:
        """Добавить компактный контекст предыдущего вопроса (для follow-up).

        Только для настоящей LLM (fallback-провайдер не рассуждает и
        может ложно сматчиться на текст контекста)."""
        if not prev or str(self.provider.name).startswith("fallback"):
            return question
        note = ["\n\n--- КОНТЕКСТ ПРЕДЫДУЩЕГО ВОПРОСА "
                "(используй ТОЛЬКО если новый вопрос — уточнение) ---",
                f'Пред. вопрос: "{prev.get("question", "")}"']
        if prev.get("plan"):
            note.append(f'Пред. план: {json.dumps(prev["plan"], ensure_ascii=False)}')
        return question + "\n".join(note)

    def _sql_fallback(self, question: str, a: Answer, reason=None,
                      prev: dict | None = None) -> Answer:
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

        # follow-up: даём предыдущий SQL, чтобы «добавь разрез»/«почему» достроили его
        q_sql = question
        if prev and prev.get("sql") and not str(self.provider.name).startswith("fallback"):
            q_sql = (question + "\n\n--- ПРЕДЫДУЩИЙ SQL "
                     f"(измени, если это доработка) ---\n{prev['sql']}")

        try:
            sql = self.provider.sql(q_sql)
        except Exception as e:
            a.error = self.layer.reject_message(f"SQL-fallback недоступен: {e}")
            return a

        # предохранитель: L2 сам сигналит, что нужных данных нет в схеме
        if "NO_DATA" in sql.upper() and "SELECT" not in sql.upper():
            a.error = self.layer.reject_message("таких данных нет в модели")
            return a

        a.sql = sql
        try:
            a.data = guardrails.safe_execute(self.con, sql)
        except Exception as first_err:
            # одна попытка самокоррекции: показываем модели текст ошибки
            fixed = self._retry_sql(question, sql, str(first_err)[:300])
            if not fixed or ("NO_DATA" in fixed.upper() and "SELECT" not in fixed.upper()):
                if isinstance(first_err, guardrails.GuardrailError):
                    a.error = f"Ad-hoc SQL отклонён guardrails: {first_err}"
                else:
                    a.error = self.layer.reject_message(
                        f"не удалось выполнить SQL: {first_err}")
                return a
            try:
                a.sql, a.retried = fixed, True
                a.data = guardrails.safe_execute(self.con, fixed)
            except Exception as e:
                a.error = self.layer.reject_message(f"не удалось выполнить SQL: {e}")
                return a

        a.explanation = ("⚠️ Это разовый расчёт, а не одна из проверенных метрик — "
                         "цифра посчитана прямо по вашему вопросу. "
                         "Перепроверьте перед использованием в своих задачах.")
        a.context = {"question": question, "sql": a.sql, "plan": None}
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
