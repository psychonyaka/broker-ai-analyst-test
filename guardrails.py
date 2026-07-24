"""
Guardrails: многослойная защита перед выполнением сгенерированного SQL.

Слои защиты (от архитектурного к техническому):
1. Архитектурный  — LLM не пишет SQL, только выбирает из semantic layer
                    (см. semantic.py: SemanticLayer.validate)
2. Статический    — SELECT-only, запрет DDL/DML, allow-list таблиц  <- этот модуль
3. Ресурсный      — принудительный LIMIT, dry-run через EXPLAIN
4. Прозрачность   — показ пользователю "как я понял вопрос" перед ответом
"""
import re

ALLOWED_TABLES = {"clients", "deposits", "trades", "marketing_spend", "targets"}
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"ATTACH|COPY|INSTALL|LOAD|PRAGMA|CALL|EXPORT)\b", re.IGNORECASE)

MAX_ROWS = 1000
MAX_SCANNED_ROWS = 5_000_000


class GuardrailError(Exception):
    """Запрос отклонен guardrails до выполнения."""


def check_sql(sql: str) -> None:
    """Статические проверки. Бросает GuardrailError при нарушении."""
    stripped = sql.strip().rstrip(";")

    # разрешаем SELECT и WITH...SELECT (CTE); DML/DDL ловит FORBIDDEN ниже
    if not (stripped.upper().startswith("SELECT")
            or stripped.upper().startswith("WITH")):
        raise GuardrailError("Разрешены только SELECT-запросы.")

    if ";" in stripped:
        raise GuardrailError("Множественные выражения запрещены.")

    if FORBIDDEN.search(stripped):
        raise GuardrailError("Обнаружена запрещенная операция (DDL/DML).")

    # allow-list таблиц: все после FROM/JOIN должно быть из белого списка.
    # Имена, введенные самим запросом (CTE из WITH, алиасы подзапросов и таблиц), —
    # легальны: их тело все равно ссылается на базовые таблицы, которые тоже
    # проверяются этим же правилом.
    #
    # Сначала «глушим» FROM внутри функций EXTRACT/SUBSTRING/TRIM/OVERLAY
    # (EXTRACT(quarter FROM col), TRIM(' ' FROM x)) — иначе их внутренний FROM
    # ловится как ссылка на таблицу и дает ложное срабатывание.
    scan = re.sub(r"\b(?:EXTRACT|SUBSTRING|SUBSTR|TRIM|OVERLAY)\s*\([^()]*\)",
                  " ", stripped, flags=re.IGNORECASE)
    local = {n.lower() for n in (
        re.findall(r"(?:WITH|,)\s+(\w+)\s+AS\s*\(", stripped, re.IGNORECASE) +
        re.findall(r"\)\s+(?:AS\s+)?(\w+)", stripped, re.IGNORECASE) +
        # алиас таблицы: FROM/JOIN <table> [AS] <alias>
        re.findall(r"\b(?:FROM|JOIN)\s+\w+\s+(?:AS\s+)?(\w+)\b", scan, re.IGNORECASE))}
    referenced = set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)",
                                scan, re.IGNORECASE))
    unknown = {t.lower() for t in referenced} - ALLOWED_TABLES - local
    if unknown:
        raise GuardrailError(f"Обращение к неразрешенным таблицам: {unknown}")


def enforce_limit(sql: str, max_rows: int = MAX_ROWS) -> str:
    """Принудительный LIMIT, чтобы не вытащить всю базу в UI."""
    if re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        return sql
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {max_rows}"


def dry_run(con, sql: str, max_scanned: int = MAX_SCANNED_ROWS) -> None:
    """
    Dry-run через EXPLAIN: оцениваем план ДО выполнения.
    Позволяет отсечь заведомо тяжелые запросы, ничего не выполняя.
    """
    try:
        plan = con.execute(f"EXPLAIN {sql}").fetchall()
    except Exception as e:
        raise GuardrailError(f"Запрос не проходит планировщик: {e}")

    plan_text = " ".join(str(r) for r in plan)
    for m in re.finditer(r"EC[:=]\s*(\d+)", plan_text):
        if int(m.group(1)) > max_scanned:
            raise GuardrailError(
                f"Запрос слишком тяжелый (оценка ~{m.group(1)} строк).")


def safe_execute(con, sql: str):
    """Полный цикл guardrails + выполнение."""
    check_sql(sql)
    sql = enforce_limit(sql)
    dry_run(con, sql)
    return con.execute(sql).fetchdf()
