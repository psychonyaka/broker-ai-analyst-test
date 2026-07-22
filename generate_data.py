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


def main():
    con = duckdb.connect(DB)
    con.execute("DROP TABLE IF EXISTS trades")
    con.execute("DROP TABLE IF EXISTS deposits")
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

    # --- сводка ---
    for t in ["clients", "deposits", "trades"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:10s} {n:>8,} rows")
    con.close()
    print(f"\nDB: {DB}")


if __name__ == "__main__":
    main()
