# Broker AI Analyst — MVP

Чат-бот, отвечающий на вопросы на естественном языке по данным брокера
(депозиты, торговый оборот, активные трейдеры) — на основе **семантического слоя**.

Домен выбран близким к брокерскому бизнесу намеренно: воронка «депозит → торговля»,
метрики оборота и активных трейдеров.

---

## TL;DR — ключевое решение

**LLM не пишет SQL.** Она возвращает *структурированный план* (какую метрику и
разрезы выбрать) из сертифицированного семантического слоя, а корректный SQL
собирает детерминированный движок. Это резко сокращает пространство ошибок модели
и делает ответы воспроизводимыми и аудируемыми — что критично для регулируемого
финтеха, где точность метрики = вопрос доверия и compliance.

```
Вопрос (NL)
  → LLM: structured output {metric, group_by, filters, order, limit}   (llm.py)
  → валидация плана по semantic layer (allow-list метрик/измерений)     (semantic.py)
  → детерминированная компиляция в SQL                                  (semantic.py)
  → guardrails: SELECT-only, allow-list таблиц, LIMIT, dry-run EXPLAIN  (guardrails.py)
  → DuckDB (read-only)
  → ответ + "как я понял вопрос" + видимый SQL                          (app.py)
```

---

## Быстрый старт

```bash
pip install -r requirements.txt
python generate_data.py          # создаёт broker.duckdb (синтетика)
streamlit run app.py             # веб-чат
# или в CLI:
python pipeline.py "топ-5 стран по обороту"
python eval.py                   # прогон качества
```

**Работает без API-ключа** — на детерминированном fallback (матчинг по синонимам
из семантического слоя). Чтобы включить настоящую LLM — скопируйте `.env.example`
в `.env` и укажите ключ (Anthropic / OpenAI) или Ollama.

---

## Архитектурные решения и trade-offs

| Решение | Почему |
|---|---|
| **Semantic layer в центре** | Метрика определена один раз (`expression + grain + built-in filters + allowed_dimensions + synonyms`). LLM ходит сюда, а не в сырую схему → нет выдуманных джойнов/грейна. Слой заодно готов быть «источником знаний» для LLM. |
| **Structured output, а не text-to-SQL** | Модель выбирает из готовых блоков; SQL детерминирован. Ошибка возможна только в выборе, не в синтаксисе/джойнах. |
| **Провайдер-агностичность** | Anthropic / OpenAI / Ollama + fallback. Нет vendor lock — та же идея, что и с headless semantic layer: знания отделены от движка. |
| **Guardrails многослойные** | Архитектурный (выбор из слоя) → статический (SELECT-only, allow-list) → ресурсный (LIMIT, dry-run EXPLAIN) → прозрачность (видимый SQL + «как я понял»). БД открыта read-only. |
| **Evaluation-харнесс** | `eval.py`: золотой тест-сет, plan_accuracy / exec_success / reject_correct. Прогоняется при смене промпта/модели — ловит регрессии, как unit-тесты. |
| **DuckDB** | Ноль setup, встроенная, колоночная. Для MVP идеально; в проде заменяется на ClickHouse/Trino — движок компиляции SQL от этого не меняется. |

---

## Что осознанно НЕ вошло в MVP (scope)

Задача без формальных требований — поэтому границы MVP я провёл сам:

- **Вошло:** semantic layer, NL→plan→SQL, guardrails, eval, чат-UI с графиками,
  прозрачность (видимый SQL), работа без ключа.
- **Не вошло (следующие шаги, но заложено архитектурно):**
  - RAG-retrieval метрик через embeddings (нужно при сотнях метрик; сейчас каталог целиком в промпт);
  - многошаговые вопросы и джойны фактов между собой;
  - MCP-tool для деплоя готового борда (следующий уровень парадигмы: заказчик → AI → деплой);
  - RLS / маскирование на уровне слоя; кэширование; расширенный eval с LLM-as-judge.

---

## Структура

```
generate_data.py     — синтетические данные брокера → broker.duckdb
semantic_layer.yaml  — семантический слой: метрики, измерения, синонимы, few-shot
semantic.py          — загрузка слоя, валидация плана, компиляция в SQL
llm.py               — провайдеры (Anthropic/OpenAI/Ollama) + fallback без ключа
guardrails.py        — SELECT-only, allow-list, LIMIT, dry-run EXPLAIN
pipeline.py          — оркестратор (вопрос → ответ), CLI
eval.py              — золотой тест-сет + метрики качества
app.py               — Streamlit чат-UI
```

---

## Пример работы (fallback, без ключа)

```
ВОПРОС: топ-5 стран по обороту
Я понял вопрос так: метрика Trading Volume, $; разрез по country; топ-5.

SELECT clients.country AS country, SUM(trades.volume_usd) AS trading_volume
FROM trades
JOIN clients ON clients.client_id = trades.client_id
WHERE (clients.is_demo = FALSE)
GROUP BY clients.country
ORDER BY trading_volume DESC
LIMIT 5
```

`python eval.py` на fallback: **plan_accuracy 90%, exec_success 100%,
reject_correct 100%** (единственный miss — вопрос на английском: fallback заточен
под русские синонимы, настоящая LLM его берёт — иллюстрация, зачем LLM поверх слоя).
