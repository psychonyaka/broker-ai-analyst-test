"""
Evaluation-харнесс: измеряем качество NL->plan, а не "на глаз".

Это то, что превращает "AI работает по ощущениям" в измеримую дисциплину.
Прогоняется при смене промпта/модели/метаданных (регрессии, как unit-тесты).

Метрики:
- plan_accuracy : доля вопросов, где метрика+разрезы совпали с эталоном
- exec_success  : доля вопросов, где итоговый SQL успешно выполнился
- reject_correct: корректно ли отклонены вопросы не по данным

Запуск: python eval.py            (текущий провайдер: LLM если есть ключ, иначе fallback)
        python eval.py fallback   (форсировать fallback)
"""
import sys

from pipeline import Chatbot

# Золотой тест-сет: вопрос -> ожидаемый (метрика, множество разрезов)
GOLDEN = [
    {"q": "сколько всего внесли депозитов",        "metric": "total_deposits", "dims": []},
    {"q": "депозиты по странам",                    "metric": "total_deposits", "dims": ["country"]},
    {"q": "deposits by country",                    "metric": "total_deposits", "dims": ["country"]},
    {"q": "средний депозит по типам счетов",         "metric": "avg_deposit",    "dims": ["account_type"]},
    {"q": "сколько активных трейдеров",              "metric": "active_traders", "dims": []},
    {"q": "торговый оборот по инструментам",         "metric": "trading_volume", "dims": ["symbol"]},
    {"q": "топ-5 стран по обороту",                  "metric": "trading_volume", "dims": ["country"], "limit": 5},
    {"q": "динамика депозитов по месяцам",            "metric": "total_deposits", "dims": ["deposit_month"]},
    {"q": "оборот по каналам привлечения",            "metric": "trading_volume", "dims": ["acquisition_channel"]},
    {"q": "число депозитов по странам",              "metric": "deposit_count",  "dims": ["country"]},
    {"q": "расходы на маркетинг по каналам",          "metric": "marketing_cost", "dims": ["mkt_channel"]},
    # --- трудные формулировки: синонимы, английский, net_pnl ---
    {"q": "выручка по странам",                       "metric": "total_deposits", "dims": ["country"]},
    {"q": "GMV по странам",                           "metric": "total_deposits", "dims": ["country"]},
    {"q": "приток средств по месяцам",                "metric": "total_deposits", "dims": ["deposit_month"]},
    {"q": "средний чек по типам счетов",              "metric": "avg_deposit",    "dims": ["account_type"]},
    {"q": "trading volume by symbol",                 "metric": "trading_volume", "dims": ["symbol"]},
    {"q": "прибыль клиентов по инструментам",         "metric": "net_pnl",        "dims": ["symbol"]},
    {"q": "затраты на рекламу по каналам",            "metric": "marketing_cost", "dims": ["mkt_channel"]},
    # --- относительные периоды (движок считает даты от CURRENT_DATE) ---
    {"q": "депозиты за последний квартал",            "metric": "total_deposits", "dims": [],
     "contains": ["CURRENT_DATE", "quarter"]},
    {"q": "оборот в прошлом месяце",                  "metric": "trading_volume", "dims": [],
     "contains": ["CURRENT_DATE", "month"]},
    {"q": "динамика депозитов за последние 3 месяца", "metric": "total_deposits", "dims": ["deposit_month"],
     "contains": ["CURRENT_DATE"]},
    {"q": "сколько депозитов с начала года",          "metric": "total_deposits", "dims": [],
     "contains": ["CURRENT_DATE", "year"]},
    # --- значения измерений (sample_values + описания): рус.название -> англ.значение ---
    {"q": "депозиты в Таиланде",                      "metric": "total_deposits", "dims": [],
     "contains": ["Thailand"]},
    {"q": "оборот по золоту",                         "metric": "trading_volume", "dims": [],
     "contains": ["XAUUSD"]},
    {"q": "оборот по нефти",                          "metric": "trading_volume", "dims": [],
     "contains": ["USOIL"]},
    {"q": "депозиты по центовым счетам",              "metric": "total_deposits", "dims": [],
     "contains": ["Standard Cent"]},
    # --- новая метрика: число клиентов (не путать с active_traders) ---
    {"q": "клиенты по странам",                       "metric": "client_count",   "dims": ["country"]},
]

# Вопросы, которые governed-слой (L1) ДОЛЖЕН отклонить (не сертифицированная метрика)
NEGATIVE = [
    "какая погода в лимассоле",
    "расскажи анекдот",
    "сколько будет 2+2",
]

# Уровень 2 (governed SQL-fallback): "длинный хвост" — вопросы про данные, которых
# нет среди сертифицированных метрик, но на них есть корректный read-only SELECT.
# Должны ОТВЕТИТЬ (ans.ok) и вернуть непустой результат.
LONGTAIL = [
    "сколько у нас стран",
    "сколько всего клиентов",
]

# Мусорные сообщения при ВКЛЮЧЁННОМ L2: система обязана отказать, а не
# сочинять SQL «по мотивам» предыдущего вопроса (класс дыр, который
# однослойный reject-тест не ловит).
CHITCHAT = [
    "ты тупой?",
    "привет, как дела",
    "что ты умеешь?",
]


def run(provider=None):
    # L1-замер: fallback выключен, чтобы измерять именно сертифицированный слой.
    bot = Chatbot(provider=provider, allow_sql_fallback=False)
    print(f"Провайдер: {bot.provider.name}\n" + "=" * 60)

    plan_ok = exec_ok = 0
    for case in GOLDEN:
        ans = bot.ask(case["q"])
        # для проверки плана переиспользуем то, что реально попало в SQL
        got_metric = ans.ok and case["metric"] in (ans.sql or "")
        got_dims = all(d in (ans.sql or "") for d in case["dims"])
        limit_ok = ("limit" not in case) or (ans.ok and f"LIMIT {case['limit']}" in ans.sql)
        # опционально: фрагменты, которые обязаны быть в SQL (напр. CURRENT_DATE)
        contains_ok = all(c in (ans.sql or "") for c in case.get("contains", []))
        ok = ans.ok and got_metric and got_dims and limit_ok and contains_ok
        plan_ok += ok
        exec_ok += ans.ok
        mark = "OK " if ok else "MISS"
        print(f"[{mark}] {case['q'][:45]:45s} -> {ans.error or ans.explanation[:40]}")

    print("-" * 60)
    rej_ok = 0
    for q in NEGATIVE:
        ans = bot.ask(q)
        good = not ans.ok  # правильно = отклонён
        rej_ok += good
        print(f"[{'OK ' if good else 'MISS'}] (reject) {q[:40]}")

    # L2-замер: длинный хвост через governed SQL-fallback (нужна настоящая LLM)
    print("-" * 60)
    bot2 = Chatbot(provider=provider, allow_sql_fallback=True)
    lt_ok = 0
    for q in LONGTAIL:
        ans = bot2.ask(q)
        good = ans.ok and ans.data is not None and len(ans.data) > 0
        lt_ok += good
        print(f"[{'OK ' if good else 'MISS'}] (L2 fallback) {q[:40]:40s} -> "
              f"{'ответ получен' if good else (ans.error[:40] if ans.error else 'нет данных')}")

    # Мусор при включённом L2 + переданном контексте (худший случай)
    print("-" * 60)
    ctx = bot2.ask("депозиты по странам").context   # заранее создаём контекст
    chat_ok = 0
    for q in CHITCHAT:
        ans = bot2.ask(q, prev_context=ctx)
        good = not ans.ok
        chat_ok += good
        print(f"[{'OK ' if good else 'MISS'}] (chitchat+L2) {q[:35]:35} -> "
              f"{'отказ' if good else 'ОТВЕТИЛ (плохо)'}")

    n = len(GOLDEN)
    print("=" * 60)
    print(f"plan_accuracy   (L1): {plan_ok}/{n}  ({plan_ok/n:.0%})")
    print(f"exec_success    (L1): {exec_ok}/{n}  ({exec_ok/n:.0%})")
    print(f"reject_correct  (L1): {rej_ok}/{len(NEGATIVE)}  ({rej_ok/len(NEGATIVE):.0%})")
    print(f"longtail_answered(L2): {lt_ok}/{len(LONGTAIL)}  ({lt_ok/len(LONGTAIL):.0%})")
    print(f"chitchat_rejected(L2): {chat_ok}/{len(CHITCHAT)}  ({chat_ok/len(CHITCHAT):.0%})")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    run(provider=sys.argv[1] if len(sys.argv) > 1 else None)
