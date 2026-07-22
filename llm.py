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
- Фильтр по году: {{"year": 2026}}. Фильтр по измерению: {{"country": "Cyprus"}}.
- Если вопрос не про данные — верни {{"metric": null, "reason": "..."}}.

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
Связи: deposits.client_id -> clients.client_id; trades.client_id -> clients.client_id"""

SQL_SYSTEM = """Ты пишешь ОДИН read-only SQL SELECT-запрос для DuckDB, отвечающий на вопрос.

{schema}

ПРАВИЛА:
- Только SELECT. Одно выражение, без ';'. Никаких INSERT/UPDATE/DELETE/DDL.
- Только таблицы clients, deposits, trades.
- По умолчанию считай реальные счета: WHERE clients.is_demo = FALSE
  (если вопрос явно не про демо-счета).
- Верни ТОЛЬКО SQL, без markdown, без пояснений."""


def sql_prompt(schema: str = SCHEMA) -> str:
    return SQL_SYSTEM.format(schema=schema)


def _extract_sql(text: str) -> str:
    """Достаём чистый SELECT из ответа модели (снимаем markdown-обёртку)."""
    text = text.strip()
    text = re.sub(r"^```(?:sql)?|```$", "", text, flags=re.MULTILINE).strip()
    m = re.search(r"(?is)\bSELECT\b.*", text)
    return (m.group(0) if m else text).strip().rstrip(";")


def _extract_json(text: str) -> dict:
    """Достаём JSON даже если модель обернула его в markdown."""
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

    def __init__(self, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.model = model

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
        """Совпадение по общей основе (учёт падежей): общий префикс >=3."""
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

        # 2. измерения — по синонимам, только разрешённые для метрики
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
