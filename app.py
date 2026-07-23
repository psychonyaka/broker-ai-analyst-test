"""
Streamlit чат-UI для AI-ассистента над данными брокера.

Запуск: streamlit run app.py

Показывает всю цепочку прозрачно: вопрос -> "как я понял" -> SQL -> данные -> график.
Прозрачность (видимый SQL + интерпретация) — это guardrail и признак доверия.
"""
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from pipeline import Chatbot
from report import auto_viz  # та же визуализация, что и в HTML-отчёте (KPI/линия/бары/хитмап)

st.set_page_config(page_title="Broker AI Analyst", page_icon="📊", layout="wide")


@st.cache_resource
def load_bot(provider, role):
    return Chatbot(provider=provider or None, role=role or None)


st.title("📊 Broker AI Analyst — MVP")
st.caption("NL-вопрос → semantic layer → structured plan → SQL → данные")

with st.sidebar:
    st.header("Настройки")
    provider = st.selectbox(
        "LLM-провайдер",
        ["(auto)", "anthropic", "openai", "ollama", "fallback"],
        help="auto: возьмёт ключ из env; fallback работает без ключа")
    provider = "" if provider == "(auto)" else provider

    # роль пользователя (RLS-lite): управляет видимостью метрик
    import semantic as _sem
    _roles = _sem.SemanticLayer("semantic_layer.yaml").roles
    role_opts = ["(все метрики)"] + list(_roles)
    role_pick = st.selectbox(
        "Роль пользователя", role_opts,
        format_func=lambda r: _roles[r]["label"] if r in _roles else r,
        help="Ролевой доступ: часть метрик скрыта от операционных ролей")
    role = "" if role_pick == "(все метрики)" else role_pick

    bot = load_bot(provider, role)
    st.success(f"Активен: **{bot.provider.name}**")

    _allowed = bot.layer.metrics_for_role(bot.role)
    _hidden = len(bot.layer.metrics) - len(_allowed)
    if role:
        if _hidden > 0:
            st.caption(f"🔒 Роль: {_roles[role]['label']} — скрыто метрик: {_hidden}")
        else:
            st.caption(f"✅ Роль: {_roles[role]['label']} — доступны все метрики")

    st.markdown("**Доступные метрики:**")
    for name, m in bot.layer.metrics.items():
        if name in _allowed:
            st.markdown(f"- {m['label']}")

    st.markdown("**Примеры вопросов:**")
    examples = ["депозиты по странам", "топ-5 стран по обороту",
                "средний депозит по типам счетов", "динамика депозитов по месяцам",
                "сколько активных трейдеров", "оборот по инструментам"]
    for ex in examples:
        if st.button(ex, key=ex, use_container_width=True):
            st.session_state["q"] = ex

def render(ans):
    if not ans.ok:
        st.error(ans.error)
        return
    if ans.data is None:          # ответ-определение из семслоя (без SQL)
        st.markdown(ans.explanation)
        return
    st.info(ans.explanation)
    with st.expander("Показать SQL"):
        st.code(ans.sql, language="sql")
    df = ans.data
    st.dataframe(df, use_container_width=True, hide_index=True)

    # авто-график: инлайновый SVG (тот же, что в HTML-отчёте). Не зависит от
    # Altair/Vega, поэтому рисуется даже там, где у Altair проблемы с сертификатами.
    viz, h = auto_viz(df, dark=True)  # Streamlit тёмный -> светлый текст на графиках
    if viz:
        components.html(viz, height=h)


# История чата + контекст последнего ответа (для multi-turn follow-up)
if "history" not in st.session_state:
    st.session_state.history = []
if "ctx" not in st.session_state:
    st.session_state.ctx = None

for past in st.session_state.history:
    with st.chat_message("user"):
        st.write(past.question)
    with st.chat_message("assistant"):
        render(past)

q = st.chat_input("Спросите про депозиты, обороты, трейдеров...") or st.session_state.pop("q", None)
if q:
    with st.chat_message("user"):
        st.write(q)
    with st.chat_message("assistant"):
        with st.spinner("Думаю..."):
            ans = bot.ask(q, prev_context=st.session_state.ctx)
        render(ans)
    st.session_state.history.append(ans)
    if ans.context:  # запоминаем контекст только успешного ответа
        st.session_state.ctx = ans.context
