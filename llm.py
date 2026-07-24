"""
Провайдер-агностичный слой LLM.

Поддержка: Anthropic (Claude), OpenAI, Ollama (локально) + детерминированный
fallback без ключа — чтобы проект запускался у любого проверяющего.

Задача LLM: вернуть СТРУКТУРИРОВАННЫЙ JSON-план (structured output), а не SQL.
Именно это ограничивает пространство ошибок: модель выбирает из готовых
сертифицированных блоков semantic layer.
"""
import json
import os
import re

try:  # авто-загрузка .env, если установлен python-dotenv
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SYSTEM_PROMPT = """Ты — аналитический ассистент брокера. Твоя задача: перевести
вопрос пользователя на естественном языке в СТРУКТУРИРОВАННЫЙ ПЛАН запроса.

Ты НЕ пишешь SQL. Ты только выбираешь из каталога ниже.

{catalog}

ПРАВИЛА:
- Выбирай ТОЛЬКО метрики и измерения из каталога, ничего не выдумывай.
- Разрезы (group_by) должны входить в allowed_dimensions выбранной метрики.
- Если пользователь просит "топ N" — ставь limit: N и order: "desc".
- СУПЕРЛАТИВЫ: "лучший/больше всего/самый крупный" -> order "desc";
  "худший/самый плохой/меньше всего/минимальный" -> order "asc".
- Фильтр по году: {{"year": 2026}}. Фильтр по измерению: {{"country": "Cyprus"}}.
- ОТНОСИТЕЛЬНЫЕ ПЕРИОДЫ (движок посчитает даты сам, не пиши их руками):
  {{"period": "last_month"}} — прошлый месяц; "this_month"; "last_quarter" —
  прошлый квартал; "this_quarter"; "last_year"; "ytd" — с начала года.
  «за последние N месяцев» -> {{"last_n_months": N}}.
- НЕ ПРИБЛИЖАЙ ПЕРИОД. Если период нельзя выразить ТОЧНО поддержанными фильтрами
  (year + относительные выше) — например «полугодие», «первое/второе полугодие»,
  «H1/H2», «с марта по июнь», конкретный диапазон месяцев/дат — НЕ подменяй его
  годом или кварталом. Верни {{"metric": null, "reason": "нестандартный период"}}
  (уйдет в L2, который умеет произвольные диапазоны дат).
- НЕ ПРИБЛИЖАЙ АГРЕГАЦИЮ. У каждой метрики агрегация фиксирована (Total/Net/оборот =
  СУММА; Average Deposit = среднее). Если запрошен ДРУГОЙ тип агрегации, которого
  нет готовой метрикой («средний/average/медиана PnL», «средний оборот НА КЛИЕНТА»,
  «на сделку», «медиана депозита») — НЕ подменяй суммой. Верни
  {{"metric": null, "reason": "нестандартная агрегация"}} (уйдет в L2).
  НО «средний депозит/средний чек» — это готовая метрика Average Deposit, бери ее.
- Если вопрос не про данные — верни {{"metric": null, "reason": "..."}}.
- ОКОННЫЕ ЗАПРОСЫ (накопительный/нарастающий итог, running total, скользящее
  среднее, доля от общего, ранг, перцентиль) — это НЕ сертифицированные метрики:
  верни {{"metric": null, "reason": "нужна оконная функция"}} (уйдет в L2,
  который умеет оконки).
- ПЛАН/ФАКТ, выполнение плана, сравнение с таргетом («выполнили ли план»,
  «план-факт по обороту/депозитам», «на сколько выполнен план», «факт vs план по
  кварталам») — НЕ сертифицированная метрика (нужны targets + разрез по кварталу):
  верни {{"metric": null, "reason": "план vs факт"}} (уйдет в L2). НЕ пытайся
  подставить сюда метрику Trading Volume/Total Deposits с фильтром quarter.
- Если спрашивают про КОНКРЕТНОГО клиента/трейдера (кто именно, какой client_id,
  «самый ... трейдер/клиент»), а среди измерений НЕТ уровня клиента — верни
  {{"metric": null, "reason": "нужен уровень клиента"}} (уйдет в SQL-fallback).
- FOLLOW-UP: если ниже дан "КОНТЕКСТ ПРЕДЫДУЩЕГО ВОПРОСА", вопрос считается
  уточнением ТОЛЬКО когда он меняет ОФОРМЛЕНИЕ предыдущего (разрез/фильтр/
  порядок/лимит): "а теперь по странам", "добавь разрез X", "убери фильтр",
  "а по месяцам", "сделай за 2025". Тогда возьми предыдущий план и измени
  ТОЛЬКО запрошенное.
  ВАЖНО: если вопрос вводит НОВУЮ метрику/показатель/понятие (напр.
  "накопительный итог", "pnl", "доля", "оборот", "средний чек") — это НОВЫЙ
  самостоятельный вопрос, ИГНОРИРУЙ предыдущий план (не тащи его разрезы),
  даже если начинается с "а"/"а можешь".

Отвечай ТОЛЬКО валидным JSON, без пояснений и markdown:
{{"metric": "...", "group_by": [...], "filters": {{}}, "order": "desc", "limit": null}}

ПРИМЕРЫ:
{few_shot}"""


def build_prompt(catalog: str, few_shot: str) -> str:
    return SYSTEM_PROMPT.format(catalog=catalog, few_shot=few_shot)


# --------------------------------------------------------------------------
# Второй уровень: governed SQL-fallback для "длинного хвоста" вопросов,
# которых нет в semantic layer (напр. "сколько у нас стран").
# LLM пишет ОДИН SELECT; его затем валидируют guardrails (SELECT-only,
# allow-list, LIMIT, dry-run). Это осознанный trade-off: покрытие в обмен
# на чуть больший риск, купированный многослойной защитой.
# --------------------------------------------------------------------------
SCHEMA = """Таблицы (DuckDB):
- clients(client_id, country, account_type, acquisition_channel, registration_date, is_demo BOOLEAN)
- deposits(deposit_id, client_id, amount_usd, deposit_date, status)  -- status: confirmed|pending|failed
- trades(trade_id, client_id, symbol, volume_usd, pnl_usd, trade_date)
- marketing_spend(spend_id, channel, spend_date, cost_usd)  -- расходы на привлечение по каналам/месяцам
- targets(target_id, metric, period, target_value)  -- квартальные ПЛАНЫ; metric in ('total_deposits','trading_volume'); period like '2025-Q1'
Связи: deposits.client_id -> clients.client_id; trades.client_id -> clients.client_id;
marketing_spend.channel -> clients.acquisition_channel (по значению канала).
Для «план vs факт»: факт по кварталу считай из deposits/trades, план бери из targets по совпадающим metric+period."""

SQL_SYSTEM = """Ты пишешь ОДИН read-only SQL SELECT-запрос для DuckDB, отвечающий на вопрос.

{schema}

ПРАВИЛА:
- Только SELECT. Одно выражение, без ';'. Никаких INSERT/UPDATE/DELETE/DDL.
- Только таблицы из схемы (clients, deposits, trades, marketing_spend, targets).
- НЕТ ДАННЫХ: если для ответа нужны данные, которых НЕТ в схеме (флаги фрода/
  мошенничества, KYC, риск-скоринг, поведенческие аномалии, данные сотрудников
  и т.п.) — верни РОВНО одно слово: NO_DATA (без SQL, без пояснений).
  НЕ подменяй отсутствующие данные похожими (напр. не выдавай PnL вместо фрода).
- НЕ ВОПРОС ПРО ДАННЫЕ: если это болтовня, приветствие, оскорбление, мнение,
  вопрос о тебе самом или что-то, не требующее выборки из БД («ты тупой?»,
  «привет», «как дела», «что ты умеешь») — верни РОВНО: NO_DATA.
  ВАЖНО: даже если ниже дан ПРЕДЫДУЩИЙ SQL — не переиспользуй его для таких
  сообщений; отсутствие смысла важнее наличия контекста.
- ПЕРСОНАЛЬНЫЕ ДАННЫЕ КЛИЕНТА: если просят выгрузить сырые записи КОНКРЕТНОГО
  клиента/человека («покажи все данные клиента с id 5», «данные клиента N»,
  «карточка/профиль клиента», «строки по client_id = X», «сделки клиента 42») —
  верни РОВНО: NO_DATA. Индивидуальные записи клиента отдавать нельзя (приватность).
  ИСКЛЮЧЕНИЕ: агрегаты и рейтинги, где client_id — РЕЗУЛЬТАТ группировки
  («самый прибыльный трейдер», «топ-N клиентов по обороту»), — разрешены.
- ПРОГНОЗ/ПРЕДСКАЗАНИЕ будущего (forecast, прогноз, предсказание, «сколько будет
  в следующем квартале») — в БД только факт, моделей прогноза нет: верни NO_DATA.
  НЕ выдавай пустой результат вместо честного отказа.
- По умолчанию считай реальные счета: WHERE clients.is_demo = FALSE
  (если вопрос явно не про демо-счета).
- "самый плохой/худший трейдер" = клиент с минимальным SUM(pnl_usd) (ORDER BY ... ASC);
  "лучший" = с максимальным. Возвращай client_id и значение.
- НАКОПИТЕЛЬНЫЙ/нарастающий итог -> SUM(...) OVER (ORDER BY <дата>).
- СКОЛЬЗЯЩЕЕ СРЕДНЕЕ за N периодов: сначала агрегируй метрику по периоду
  (месяцу) в CTE/подзапросе, затем окно
  AVG(<агрегат>) OVER (ORDER BY <период> ROWS BETWEEN N-1 PRECEDING AND CURRENT ROW).
- ДОЛЯ топ-N: ЗНАМЕНАТЕЛЬ — сумма по ВСЕМ (без ограничения топ-N), ЧИСЛИТЕЛЬ —
  сумма по топ-N. Шаблон:
  SELECT 100.0 * (SELECT SUM(volume_usd) FROM trades WHERE client_id IN
    (SELECT client_id FROM trades GROUP BY client_id ORDER BY SUM(volume_usd) DESC LIMIT 3))
    / (SELECT SUM(volume_usd) FROM trades) AS top3_share
- ПЛАН vs ФАКТ: квартал из даты =
  (EXTRACT(year FROM deposit_date)::VARCHAR || '-Q' || EXTRACT(quarter FROM deposit_date)::VARCHAR).
  Джойни факт по кварталу с targets ON targets.period = <квартал>
  AND targets.metric = 'total_deposits' (или 'trading_volume' для оборота).
- FOLLOW-UP: если ниже дан "ПРЕДЫДУЩИЙ SQL" и вопрос — доработка ("добавь разрез
  стран", "а теперь по X", "почему", "детализируй"), то ИЗМЕНИ предыдущий SQL,
  а не пиши с нуля. "почему" — разбей предыдущий показатель на составляющие.
- Верни ТОЛЬКО SQL (или NO_DATA), без markdown, без пояснений."""


def sql_prompt(schema: str = SCHEMA) -> str:
    return SQL_SYSTEM.format(schema=schema)


def _extract_sql(text: str) -> str:
    """Достаем чистый запрос из ответа модели (снимаем markdown-обертку).

    Начинаем с WITH или SELECT — что раньше: иначе у CTE-запросов
    (WITH x AS (...) SELECT ...) срезался бы префикс WITH и SQL ломался.
    """
    text = text.strip()
    text = re.sub(r"^```(?:sql)?|```$", "", text, flags=re.MULTILINE).strip()
    m = re.search(r"(?is)\b(?:WITH|SELECT)\b.*", text)
    return (m.group(0) if m else text).strip().rstrip(";")


def _extract_json(text: str) -> dict:
    """Достаем JSON даже если модель обернула его в markdown."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"Модель вернула не-JSON: {text[:200]}")
    return json.loads(match.group(0))


# --------------------------------------------------------------------------
# Провайдеры
# --------------------------------------------------------------------------
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = model

    def plan(self, question: str, system: str) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        return _extract_json(resp.content[0].text)

    def sql(self, question: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            system=sql_prompt(),
            messages=[{"role": "user", "content": question}],
        )
        return _extract_sql(resp.content[0].text)


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str | None = None):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        # модель настраивается через .env (OPENAI_MODEL); по умолчанию — gpt-4o
        # (заметно лучше понимает разговорные формулировки, чем -mini)
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o")

    def plan(self, question: str, system: str) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": question}],
        )
        return _extract_json(resp.choices[0].message.content)

    def sql(self, question: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": sql_prompt()},
                      {"role": "user", "content": question}],
        )
        return _extract_sql(resp.choices[0].message.content)


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1")
        self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def plan(self, question: str, system: str) -> dict:
        import requests
        r = requests.post(
            f"{self.host}/api/chat",
            json={"model": self.model, "stream": False, "format": "json",
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": question}]},
            timeout=120,
        )
        r.raise_for_status()
        return _extract_json(r.json()["message"]["content"])

    def sql(self, question: str) -> str:
        import requests
        r = requests.post(
            f"{self.host}/api/chat",
            json={"model": self.model, "stream": False,
                  "messages": [{"role": "system", "content": sql_prompt()},
                               {"role": "user", "content": question}]},
            timeout=120,
        )
        r.raise_for_status()
        return _extract_sql(r.json()["message"]["content"])


class FallbackProvider:
    """
    Детерминированный резерв без LLM: матчинг по синонимам из semantic layer.
    Нужен, чтобы демо работало без API-ключа (и как безопасный fallback в проде,
    если провайдер недоступен).
    """
    name = "fallback (no LLM)"

    def __init__(self, layer):
        self.layer = layer

    @staticmethod
    def _norm(s: str) -> str:
        return s.lower().replace("ё", "е")

    @classmethod
    def _word_match(cls, a: str, b: str) -> bool:
        """Совпадение по общей основе (учет падежей): общий префикс >=3."""
        a, b = cls._norm(a), cls._norm(b)
        if a == b:
            return True
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n >= 3 and n >= min(len(a), len(b)) - 2

    @classmethod
    def _matches(cls, synonym: str, words: list[str]) -> bool:
        """Матч, если ВСЕ значимые слова синонима (>=3 букв) есть в вопросе."""
        tokens = [t for t in re.findall(r"\w+", synonym.lower()) if len(t) >= 3]
        if not tokens:
            return False
        return all(any(cls._word_match(tok, w) for w in words) for tok in tokens)

    def plan(self, question: str, system: str = "") -> dict:
        q = question.lower()
        words = re.findall(r"\w+", q)

        # 1. метрика — по синонимам и имени (лучший = самый длинный матч)
        best, best_len = None, 0
        for name, m in self.layer.metrics.items():
            for token in [name, *m.get("synonyms", [])]:
                if self._matches(token, words) and len(token) > best_len:
                    best, best_len = name, len(token)
        if not best:
            return {"metric": None, "reason": "не распознана метрика"}

        # 2. измерения — по синонимам, только разрешенные для метрики
        allowed = self.layer.metrics[best]["allowed_dimensions"]
        group_by = []
        for dim in allowed:
            spec = self.layer.dimensions[dim]
            for token in [dim, *spec.get("synonyms", [])]:
                if self._matches(token, words):
                    group_by.append(dim)
                    break

        # 3. год и топ-N
        filters = {}
        if (year := re.search(r"\b(20\d{2})\b", q)):
            filters["year"] = int(year.group(1))
        limit = None
        if (top := re.search(r"топ[- ]?(\d+)|top[- ]?(\d+)", q)):
            limit = int(top.group(1) or top.group(2))

        return {"metric": best, "group_by": group_by, "filters": filters,
                "order": "desc", "limit": limit}


def get_provider(layer, prefer: str | None = None):
    """
    Автовыбор провайдера: явное указание -> доступный ключ -> Ollama -> fallback.
    Провайдер-агностичность = отсутствие vendor lock (та же логика, что и с
    semantic layer: знания отделены от движка).
    """
    prefer = prefer or os.environ.get("LLM_PROVIDER")
    try:
        if prefer == "anthropic":
            return AnthropicProvider()
        if prefer == "openai":
            return OpenAIProvider()
        if prefer == "ollama":
            return OllamaProvider()
        if prefer == "fallback":
            return FallbackProvider(layer)
        if os.environ.get("ANTHROPIC_API_KEY"):
            return AnthropicProvider()
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAIProvider()
    except Exception as e:
        print(f"[warn] Провайдер недоступен ({e}); переключаюсь на fallback.")
    return FallbackProvider(layer)
