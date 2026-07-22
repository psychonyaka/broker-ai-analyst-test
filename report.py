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


# --------------------------------------------------------------------------
# Фирменная палитра (в духе Exness: жёлтый + тёмный). Только цвета, без
# логотипа/названия — визуал «в бренде», но не имитирует компанию.
# --------------------------------------------------------------------------
DARK = "#1B1D24"        # тёмный фон карточек
ACCENT = "#FFD200"      # фирменный жёлтый (на тёмном)
DATA = "#E8B400"        # золотой для данных (читается и на белом, и на тёмном)
INK = "#1B1D24"         # основной текст (светлый фон)
MUTED = "#6B7280"       # приглушённый текст (светлый фон)
GRID = "#E5E7EB"        # линии сетки (светлый фон)
# Тёмная тема (Streamlit): текст должен быть светлым, иначе не виден
INK_D = "#E8EAED"
MUTED_D = "#AAB0BC"
GRID_D = "#3A3D46"


def _ink(dark):   return INK_D if dark else INK
def _muted(dark): return MUTED_D if dark else MUTED
def _grid(dark):  return GRID_D if dark else GRID


def _svg_bar_chart(df: pd.DataFrame, width: int = 640, bar_h: int = 26,
                   dark: bool = False) -> str:
    """Простой горизонтальный bar chart как инлайновый SVG (без зависимостей)."""
    if df.shape[1] != 2 or len(df) < 1:
        return ""
    dim, metric = df.columns
    if not pd.api.types.is_numeric_dtype(df[metric]):
        return ""
    ink, muted = _ink(dark), _muted(dark)

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
            f'fill="{ink}">{lab_esc}</text>'
            f'<rect x="{label_w + 8}" y="{y}" width="{w}" height="{bar_h}" '
            f'rx="4" fill="{DATA}"><title>{lab_esc}: {val_txt}</title></rect>'
            f'<text x="{label_w + 16 + w}" y="{y + bar_h * 0.7}" '
            f'fill="{muted}">{val_txt}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_line_chart(df: pd.DataFrame, width: int = 640, height: int = 240,
                    dark: bool = False) -> str:
    """Линейный график для временных рядов (динамика по датам/месяцам)."""
    dim, metric = df.columns
    vals = df[metric].tolist()
    labels = df[dim].astype(str).tolist()
    n = len(vals)
    if n < 2:
        return ""
    muted, grid = _muted(dark), _grid(dark)
    pad_l, pad_r, pad_t, pad_b = 60, 20, 16, 40
    vmax, vmin = max(vals), min(vals)
    span = (vmax - vmin) or 1
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x(i):
        return pad_l + plot_w * i / (n - 1)

    def y(v):
        return pad_t + plot_h * (1 - (v - vmin) / span)

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" fill="{DATA}">'
        f'<title>{html.escape(labels[i])}: {v:,.2f}</title></circle>'
        for i, v in enumerate(vals))
    # подписи оси X: показываем ~6 равномерно
    step = max(1, n // 6)
    xlabels = "".join(
        f'<text x="{x(i):.1f}" y="{height - pad_b + 16}" text-anchor="middle" '
        f'fill="{muted}" font-size="10">{html.escape(labels[i][:10])}</text>'
        for i in range(0, n, step))
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="system-ui,Segoe UI,Arial">'
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height-pad_b}" stroke="{grid}"/>'
        f'<line x1="{pad_l}" y1="{height-pad_b}" x2="{width-pad_r}" y2="{height-pad_b}" stroke="{grid}"/>'
        f'<text x="{pad_l-8}" y="{y(vmax)+4:.1f}" text-anchor="end" fill="{muted}" '
        f'font-size="10">{vmax:,.0f}</text>'
        f'<text x="{pad_l-8}" y="{y(vmin)+4:.1f}" text-anchor="end" fill="{muted}" '
        f'font-size="10">{vmin:,.0f}</text>'
        f'<polyline points="{pts}" fill="none" stroke="{DATA}" stroke-width="2"/>'
        f'{dots}{xlabels}</svg>')


def _is_temporal(series: pd.Series, name: str) -> bool:
    """Похоже ли измерение на время (дата/месяц) — тогда рисуем линию."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    return any(k in name.lower() for k in ("month", "date", "месяц", "дата"))


def _fmt(v) -> str:
    """Число -> человекочитаемо: 1 234 567 или 12.3K/4.5M для крупных."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if abs(v) >= 10_000:
        return f"{v/1_000:.0f}K"
    if v == int(v):
        return f"{int(v):,}".replace(",", " ")
    return f"{v:,.2f}".replace(",", " ")


def _kpi_card(value, label: str) -> str:
    """KPI-карточка: одно крупное число (когда в ответе одно значение)."""
    return (
        f'<div style="background:{DARK};border-radius:12px;padding:22px 28px;'
        f'display:inline-block;min-width:200px;font-family:system-ui,Segoe UI,Arial">'
        f'<div style="color:{ACCENT};font-size:40px;font-weight:700;line-height:1.1">'
        f'{_fmt(value)}</div>'
        f'<div style="color:#C9CDD6;font-size:13px;margin-top:6px">{html.escape(label)}</div>'
        f'</div>')


def _svg_heatmap(df: pd.DataFrame, cell: int = 46, dark: bool = False) -> str:
    """Хитмап для двух разрезов: dim1 (строки) x dim2 (столбцы), цвет = мера.

    Полезно, когда вопрос имеет два измерения (напр. «оборот по странам и
    типам счетов»). Интенсивность жёлтого = величина метрики."""
    ink, grid = _ink(dark), _grid(dark)
    dim1, dim2, metric = df.columns
    piv = df.pivot_table(index=dim1, columns=dim2, values=metric, aggfunc="sum")
    rows = [str(r) for r in piv.index][:12]
    cols = [str(c) for c in piv.columns][:12]
    vmax = piv.values.max() or 1
    lab_w, top_h = 130, 70
    width = lab_w + len(cols) * cell + 10
    height = top_h + len(rows) * cell + 10
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'style="max-width:{width}px" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="system-ui,Segoe UI,Arial" font-size="11">']
    # заголовки столбцов (под наклоном)
    for j, c in enumerate(cols):
        cx = lab_w + j * cell + cell / 2
        parts.append(f'<text x="{cx:.0f}" y="{top_h-8}" text-anchor="end" '
                     f'transform="rotate(-40 {cx:.0f} {top_h-8})" fill="{ink}">'
                     f'{html.escape(c[:14])}</text>')
    for i, r in enumerate(rows):
        cy = top_h + i * cell
        parts.append(f'<text x="{lab_w-8}" y="{cy+cell*0.6:.0f}" text-anchor="end" '
                     f'fill="{ink}">{html.escape(r[:16])}</text>')
        for j, c in enumerate(cols):
            val = piv.loc[piv.index[i], piv.columns[j]]
            val = 0 if pd.isna(val) else val
            t = (val / vmax) ** 0.6  # gamma для читаемости слабых значений
            # интерполяция белый -> жёлтый (ACCENT)
            rr = int(255 + (255 - 255) * t); gg = int(255 - (255 - 210) * t)
            bb = int(255 - (255 - 0) * t)
            cx = lab_w + j * cell
            txt = "#1B1D24" if t > 0.35 else "#9AA0AA"
            parts.append(
                f'<rect x="{cx}" y="{cy}" width="{cell-3}" height="{cell-3}" rx="3" '
                f'fill="rgb({rr},{gg},{bb})" stroke="{grid}">'
                f'<title>{html.escape(r)} / {html.escape(c)}: {_fmt(val)}</title></rect>'
                f'<text x="{cx+(cell-3)/2:.0f}" y="{cy+cell*0.6:.0f}" '
                f'text-anchor="middle" fill="{txt}">{_fmt(val)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def auto_viz(df: pd.DataFrame, dark: bool = False):
    """Авто-выбор визуализации по форме данных. Возвращает (html, высота_px).

    dark=True — светлый текст для тёмного фона (Streamlit); False — тёмный
    текст для светлого HTML-отчёта.

    - одно число            -> KPI-карточка
    - время + мера          -> линейный график
    - категория + мера      -> барчарт
    - два разреза + мера     -> хитмап
    - иначе                 -> ("", 0), показываем только таблицу
    """
    if df is None or df.empty:
        return "", 0
    n, ncols = len(df), df.shape[1]
    # одно значение -> KPI (карточка тёмная сама по себе — тема не важна)
    if n == 1 and ncols == 1 and pd.api.types.is_numeric_dtype(df.iloc[:, 0]):
        return _kpi_card(df.iloc[0, 0], str(df.columns[0])), 150
    if ncols == 2 and pd.api.types.is_numeric_dtype(df.iloc[:, 1]):
        if n == 1:
            return _kpi_card(df.iloc[0, 1], str(df.iloc[0, 0])), 150
        dim = df.columns[0]
        if _is_temporal(df[dim], str(dim)):
            return _svg_line_chart(df, dark=dark), 260
        return _svg_bar_chart(df, dark=dark), min(n, 15) * 34 + 24
    # два разреза + мера -> хитмап
    if ncols == 3 and pd.api.types.is_numeric_dtype(df.iloc[:, 2]):
        try:
            hm = _svg_heatmap(df, dark=dark)
            r = min(df.iloc[:, 0].nunique(), 12)
            return hm, 70 + r * 46 + 20
        except Exception:
            return "", 0
    return "", 0


def _svg_chart(df: pd.DataFrame, dark: bool = False) -> str:
    """Совместимость: вернуть только разметку визуализации (без высоты)."""
    html_out, _ = auto_viz(df, dark=dark)
    return html_out


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
        chart = _svg_chart(ans.data)
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
