"""
MCP-сервер: экспонирует semantic layer и бота как инструменты для агента.

Зачем это здесь: в агентной архитектуре (Azure AI Foundry, Claude Desktop и т.п.)
оркестратор дёргает не «сырой SQL», а ИНСТРУМЕНТЫ через MCP (Model Context
Protocol). Этот сервер — ровно тот "MCP-tool: query_metrics", который в
проектной архитектуре описан как главный путь агента к governed-семантике.

Инструменты:
- list_metrics()            — какой словарь метрик доступен (grounding для агента)
- ask(question)             — полный NL->plan->SQL->данные с guardrails
- query_metric(metric, ...) — прямой governed-вызов метрики (без LLM)

Запуск (stdio-транспорт, как ждёт Claude Desktop / любой MCP-клиент):
    pip install "mcp[cli]"
    python mcp_server.py

Подключение в Claude Desktop (claude_desktop_config.json):
    {
      "mcpServers": {
        "broker-analyst": {
          "command": "python",
          "args": ["C:/ClaudeLocal/exness-chatbot/mcp_server.py"]
        }
      }
    }

Примечание: MCP — опциональная витрина архитектуры. Ядро (semantic layer,
guardrails, eval, Streamlit) работает и без неё.
"""
import json

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # держим проект запускаемым даже без пакета mcp
    raise SystemExit(
        "Не установлен пакет MCP. Установите:  pip install \"mcp[cli]\"\n"
        "MCP — опциональный слой; ядро проекта работает без него "
        "(pipeline.py / app.py / eval.py).")

from pipeline import Chatbot
from semantic import QueryPlan

mcp = FastMCP("broker-analyst")
_bot = Chatbot()  # переиспользуем полный конвейер (semantic + guardrails)


@mcp.tool()
def list_metrics() -> str:
    """Список сертифицированных метрик с описанием и разрешёнными разрезами.

    Агент вызывает это, чтобы знать доступный словарь (grounding), прежде чем
    задавать вопросы — как каталог инструментов."""
    out = []
    for name, m in _bot.layer.metrics.items():
        out.append({
            "metric": name,
            "label": m["label"],
            "description": m["description"].strip(),
            "allowed_dimensions": m["allowed_dimensions"],
        })
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool()
def ask(question: str) -> str:
    """Ответить на вопрос на естественном языке по данным брокера.

    Полный governed-конвейер: LLM выбирает метрику из semantic layer ->
    детерминированная компиляция в SQL -> guardrails (SELECT-only, LIMIT,
    dry-run) -> выполнение. Возвращает интерпретацию, SQL и данные."""
    ans = _bot.ask(question)
    if not ans.ok:
        return json.dumps({"ok": False, "error": ans.error}, ensure_ascii=False)
    return json.dumps({
        "ok": True,
        "interpretation": ans.explanation,
        "sql": ans.sql,
        "rows": ans.data.to_dict(orient="records"),
    }, ensure_ascii=False, default=str)


@mcp.tool()
def query_metric(metric: str, group_by: list[str] | None = None,
                 limit: int | None = None) -> str:
    """Прямой governed-вызов метрики БЕЗ LLM (детерминированный путь).

    Полезно, когда агент уже знает метрику из list_metrics() — тогда обращение
    идёт без риска NL-разбора. План валидируется по semantic layer и
    прогоняется через те же guardrails."""
    plan = QueryPlan(metric=metric, group_by=group_by or [], limit=limit)
    if errors := _bot.layer.validate(plan):
        return json.dumps({"ok": False, "error": "; ".join(errors)},
                          ensure_ascii=False)
    import guardrails
    sql = _bot.layer.compile_sql(plan)
    try:
        df = guardrails.safe_execute(_bot.con, sql)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    return json.dumps({"ok": True, "sql": sql,
                       "rows": df.to_dict(orient="records")},
                      ensure_ascii=False, default=str)


if __name__ == "__main__":
    mcp.run()
