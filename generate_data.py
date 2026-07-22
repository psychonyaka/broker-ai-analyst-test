"""
Генерация синтетических данных брокера в DuckDB.
Домен: онлайн-брокер (трейдеры, депозиты, сделки) — близко к Exness.

Запуск: python generate_data.py  ->  создаёт broker.duckdb
"""
import duckdb
import random
from datetime import date, timedelta

random.seed(42)
DB = "broker.duckdb"

COUNTRIES = ["Cyprus", "Thailand", "South Africa", "Indonesia", "Nigeria",
             "Vietnam", "UAE", "India", "Brazil", "Kenya"]
ACCOUNT_TYPES = ["Standard", "Standard Cent", "Pro", "Raw Spread", "Zero"]
CHANNELS = ["organic", "google_ads", "facebook", "affiliate", "referral"]
SYMBOLS = ["EURUSD", "XAUUSD", "BTCUSD", "GBPUSD", "USOIL", "US500", "AAPL"]
STATUSES = ["confirmed", "confirmed", "confirmed", "confirmed", "pending", "failed"]


def daterange(start, end):
    return start + timedelta(days=random.randint(0, (end - start).days))


def month_starts(start, end):
    """Список первых чисел месяцев в диапазоне [start, end]."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def quarter_of(d):
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def main():
    con = duckdb.connect(DB)
    con.execute("DROP TABLE IF EXISTS trades")
    con.execute("DROP TABLE IF EXISTS deposits")
    con.execute("DROP TABLE IF EXISTS marketing_spend")
    con.execute("DROP TABLE IF EXISTS targets")
    con.execute("DROP TABLE IF EXISTS clients")

    con.execute("""
        CREATE TABLE clients (
            client_id          INTEGER PRIMARY KEY,
            country            VARCHAR,
            account_type       VARCHAR,
            acquisition_channel VARCHAR,
            registration_date  DATE,
            is_demo            BOOLEAN
        )""")
    con.execute("""
        CREATE TABLE deposits (
            deposit_id   INTEGER PRIMARY KEY,
            client_id    INTEGER,
            amount_usd   DOUBLE,
            deposit_date DATE,
            status       VARCHAR
        )""")
    con.execute("""
        CREATE TABLE trades (
            trade_id    INTEGER PRIMARY KEY,
            client_id   INTEGER,
            symbol      VARCHAR,
            volume_usd  DOUBLE,
            pnl_usd     DOUBLE,
            trade_date  DATE
        )""")
    # Расходы на маркетинг по каналам и месяцам (для "расходная часть", CAC)
    con.execute("""
        CREATE TABLE marketing_spend (
            spend_id   INTEGER PRIMARY KEY,
            channel    VARCHAR,
            spend_date DATE,
            cost_usd   DOUBLE
        )""")
    # Квартальные планы по метрикам (для "укладываемся ли в план?", план vs факт)
    con.execute("""
        CREATE TABLE targets (
            target_id    INTEGER PRIMARY KEY,
            metric       VARCHAR,
            period       VARCHAR,
            target_value DOUBLE
        )""")

    start, end = date(2025, 1, 1), date(2026, 6, 30)

    # --- clients ---
    clients = []
    for cid in range(1, 2001):
        clients.append((
            cid,
            random.choice(COUNTRIES),
            random.choices(ACCOUNT_TYPES, weights=[35, 15, 25, 15, 10])[0],
            random.choices(CHANNELS, weights=[30, 25, 20, 15, 10])[0],
            daterange(start, end),
            random.random() < 0.15,  # 15% demo
        ))
    con.executemany("INSERT INTO clients VALUES (?,?,?,?,?,?)", clients)

    # --- deposits --- (только не-демо клиенты вносят деньги)
    real_clients = [c for c in clients if not c[5]]
    deposits, did = [], 1
    for c in real_clients:
        n = random.choices([0, 1, 2, 3, 5, 10], weights=[15, 30, 25, 15, 10, 5])[0]
        for _ in range(n):
            amt = round(random.choice([50, 100, 250, 500, 1000, 5000]) *
                        random.uniform(0.8, 1.5), 2)
            deposits.append((did, c[0], amt,
                             daterange(c[4], end),
                             random.choice(STATUSES)))
            did += 1
    con.executemany("INSERT INTO deposits VALUES (?,?,?,?,?)", deposits)

    # --- trades --- (активные трейдеры = те, кто внёс подтверждённый депозит)
    depositors = {d[1] for d in deposits if d[4] == "confirmed"}
    trades, tid = [], 1
    for cid in depositors:
        n = random.choices([0, 5, 20, 50, 200], weights=[20, 30, 25, 15, 10])[0]
        for _ in range(n):
            vol = round(random.uniform(100, 50000), 2)
            pnl = round(vol * random.uniform(-0.05, 0.04), 2)
            trades.append((tid, cid, random.choice(SYMBOLS), vol, pnl,
                           daterange(start, end)))
            tid += 1
    con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?)", trades)

    # --- marketing_spend --- (расходы по каналам помесячно; organic ~бесплатно)
    channel_budget = {"organic": 0, "google_ads": 40000, "facebook": 30000,
                      "affiliate": 15000, "referral": 5000}
    spend, sid = [], 1
    for mstart in month_starts(start, end):
        for ch, base in channel_budget.items():
            if base == 0:
                continue
            cost = round(base * random.uniform(0.7, 1.3), 2)
            spend.append((sid, ch, mstart, cost))
            sid += 1
    con.executemany("INSERT INTO marketing_spend VALUES (?,?,?,?)", spend)

    # --- targets --- (квартальные планы по депозитам и обороту; факт то выше, то ниже)
    quarters = sorted({quarter_of(m) for m in month_starts(start, end)})
    targets, gid = [], 1
    for qi, q in enumerate(quarters):
        # растущий план; факт местами не дотягивает -> интересный "план vs факт"
        targets.append((gid, "total_deposits", q, round(900_000 + qi * 60_000, 2)))
        gid += 1
        targets.append((gid, "trading_volume", q, round(150_000_000 + qi * 12_000_000, 2)))
        gid += 1
    con.executemany("INSERT INTO targets VALUES (?,?,?,?)", targets)

    # --- сводка ---
    for t in ["clients", "deposits", "trades", "marketing_spend", "targets"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:10s} {n:>8,} rows")
    con.close()
    print(f"\nDB: {DB}")


if __name__ == "__main__":
    main()
