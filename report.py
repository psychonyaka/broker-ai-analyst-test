"""
Генерация self-contained HTML-отчёта из ответа бота.

Зачем: агенту в реальной архитектуре мало «вернуть таблицу» — он отдаёт
готовый артефакт (report / board), который открывается двойным кликом без
установки чего-либо. Один файл: вопрос -> "как я понял" -> SQL -> таблица ->
инлайновый SVG-график. Никаких внешних библиотек и CDN (важно для доверия
и для окружений без интернета).

CLI:  python report.py "топ-5 стран по обороту"  ->  report.html
"""
import html
import re
import sys
import webbrowser

import pandas as pd

from pipeline import Chatbot


def _svg_bar_chart(df: pd.DataFrame, width: int = 640, bar_h: int = 26) -> str:
    """Простой горизонтальный bar chart как инлайновый SVG (без зависимостей)."""
    if df.shape[1] != 2 or len(df) < 1:
        return ""
    dim, metric = df.columns
    if not pd.api.types.is_numeric_dtype(df[metric]):
        return ""

    rows = df.head(15).copy()
    labels = rows[dim].astype(str).tolist()
    values = rows[metric].tolist()
    vmax = max(values) or 1
    label_w, pad, chart_w = 160, 8, 260
    height = len(rows) * (bar_h + pad) + pad

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'style="max-width:{width}px" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="system-ui,Segoe UI,Arial" font-size="13">']
    for i, (lab, val) in enumerate(zip(labels, values)):
        y = pad + i * (bar_h + pad)
        w = max(1, int(chart_w * (val / vmax)))
        lab_esc = html.escape(lab[:22])
        val_txt = f"{val:,.0f}".replace(",", " ")
        parts.append(
            f'<text x="{label_w}" y="{y + bar_h * 0.7}" text-anchor="end" '
            f'fill="#333">{lab_esc}</text>'
            f'<rect x="{label_w + 8}" y="{y}" width="{w}" height="{bar_h}" '
            f'rx="4" fill="#2f6fed"/>'
            f'<text x="{label_w + 16 + w}" y="{y + bar_h * 0.7}" '
            f'fill="#555">{val_txt}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _table(df: pd.DataFrame) -> str:
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = []
    for _, row in df.head(100).iterrows():
        cells = "".join(f"<td>{html.escape(str(v))}</td>" for v in row)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


HTML_TMPL = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Broker AI Analyst — отчёт</title>
<style>
  body {{ font-family: system-ui, "Segoe UI", Arial, sans-serif; color:#1a1a1a;
         max-width: 820px; margin: 40px auto; padding: 0 20px; line-height:1.5; }}
  .q {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  .explain {{ background:#eef4ff; border-left:4px solid #2f6fed; padding:10px 14px;
              border-radius:6px; margin: 14px 0; }}
  details {{ margin: 14px 0; }}
  pre {{ background:#0f172a; color:#e2e8f0; padding:14px; border-radius:8px;
         overflow-x:auto; font-size:13px; }}
  table {{ border-collapse: collapse; width:100%; margin-top:10px; font-size:14px; }}
  th, td {{ border:1px solid #e2e8f0; padding:6px 10px; text-align:left; }}
  th {{ background:#f8fafc; }}
  .muted {{ color:#64748b; font-size:13px; }}
  .err {{ background:#fef2f2; border-left:4px solid #dc2626; padding:12px 14px;
          border-radius:6px; white-space:pre-wrap; }}
  h1 {{ font-size: 22px; }}
</style></head><body>
<h1>📊 Broker AI Analyst</h1>
<p class="muted">NL-вопрос → semantic layer → structured plan → SQL → данные.
Провайдер: <b>{provider}</b></p>
<div class="q">❓ {question}</div>
{content}
<p class="muted">Сгенерировано автоматически. LLM выбирает из сертифицированных
метрик, SQL собирает детерминированный движок.</p>
</body></html>"""


def render_html(ans) -> str:
    if not ans.ok:
        content = f'<div class="err">{html.escape(ans.error)}</div>'
    else:
        chart = _svg_bar_chart(ans.data)
        # markdown-жирный (**x**) из explanation -> <b>x</b> (после экранирования)
        explain = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(ans.explanation))
        content = (
            f'<div class="explain">{explain}</div>'
            f'<details open><summary>SQL</summary>'
            f'<pre>{html.escape(ans.sql)}</pre></details>'
            f'{chart}'
            f'{_table(ans.data)}')
    return HTML_TMPL.format(
        provider=html.escape(ans.provider),
        question=html.escape(ans.question),
        content=content)


def main():
    question = " ".join(sys.argv[1:]) or "топ-5 стран по обороту"
    out = "report.html"
    bot = Chatbot()
    ans = bot.ask(question)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(ans))
    print(f"Отчёт сохранён: {out}  (провайдер: {ans.provider})")
    try:
        webbrowser.open(out)
    except Exception:
        pass


if __name__ == "__main__":
    main()
