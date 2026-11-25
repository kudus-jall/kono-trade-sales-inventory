import re


AUTO_ID_TABLES = {
    "businesses",
    "users",
    "products",
    "product_units",
    "customers",
    "suppliers",
    "purchases",
    "supplier_returns",
    "sales",
    "sales_returns",
    "password_reset_tokens",
    "password_reset_requests",
    "payments",
    "expenses",
    "audit_logs",
    "notifications",
}


class HybridRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            key = tuple(self.keys())[key]
        return super().__getitem__(key)


def postgres_row_factory(cursor):
    columns = tuple(column.name for column in cursor.description)

    def make_row(values):
        return HybridRow(zip(columns, values))

    return make_row


def postgres_schema(schema):
    schema = re.sub(r"(?m)^\s*PRAGMA foreign_keys\s*=\s*ON;\s*", "", schema)
    schema = re.sub(r"\bINTEGER\b", "BIGINT", schema, flags=re.IGNORECASE)
    schema = re.sub(
        r"\bid\s+BIGINT\s+PRIMARY\s+KEY\b",
        "id BIGSERIAL PRIMARY KEY",
        schema,
        flags=re.IGNORECASE,
    )
    return schema


def postgres_sql(statement):
    sql = statement.strip().rstrip(";")
    ignored_insert = bool(
        re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\b", sql)
    )
    if ignored_insert:
        sql = re.sub(
            r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            sql,
            count=1,
        )
        if not re.search(r"(?is)\bON\s+CONFLICT\b", sql):
            sql += " ON CONFLICT DO NOTHING"

    sql = re.sub(
        r"(?is)date\(\s*'now'\s*,\s*'-(\d+)\s+days?'\s*\)",
        r"(CURRENT_DATE - INTERVAL '\1 days')",
        sql,
    )
    sql = re.sub(
        r"(?i)\bdate\(\s*([a-z_][a-z0-9_.]*)\s*\)",
        r"substring(\1, 1, 10)::date",
        sql,
    )
    sql = sql.replace("?", "%s")

    insert = re.match(r"(?is)^INSERT\s+INTO\s+([a-z_][a-z0-9_]*)\b", sql)
    returns_id = bool(
        insert
        and insert.group(1).lower() in AUTO_ID_TABLES
        and not re.search(r"(?is)\bRETURNING\b", sql)
    )
    if returns_id:
        sql += " RETURNING id"
    return sql, returns_id


class PostgresCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    @property
    def in_transaction(self):
        return self._connection.info.transaction_status.name != "IDLE"

    def execute(self, statement, parameters=()):
        sql, returns_id = postgres_sql(statement)
        converted_parameters = tuple(
            int(value) if isinstance(value, bool) else value
            for value in (parameters or ())
        )
        cursor = self._connection.execute(sql, converted_parameters)
        lastrowid = None
        if returns_id and cursor.description:
            row = cursor.fetchone()
            if row is not None:
                lastrowid = row["id"]
        return PostgresCursor(cursor, lastrowid)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()
