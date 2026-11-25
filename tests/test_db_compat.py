import unittest
from types import SimpleNamespace

from backup_db import postgres_environment, postgres_database_name
from db_compat import (
    HybridRow,
    PostgresConnection,
    postgres_row_factory,
    postgres_schema,
    postgres_sql,
)


class PostgresCompatibilityTests(unittest.TestCase):
    def test_schema_uses_postgres_identity_columns_and_bigint_references(self):
        schema = postgres_schema(
            """PRAGMA foreign_keys = ON;
            CREATE TABLE businesses (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                business_id INTEGER NOT NULL REFERENCES businesses(id)
            );"""
        )

        self.assertNotIn("PRAGMA", schema)
        self.assertEqual(schema.count("id BIGSERIAL PRIMARY KEY"), 2)
        self.assertIn("business_id BIGINT NOT NULL REFERENCES businesses(id)", schema)

    def test_sql_converts_placeholders_ignore_insert_and_date_functions(self):
        sql, returns_id = postgres_sql(
            "INSERT OR IGNORE INTO notifications (user_id) VALUES (?)"
        )
        self.assertEqual(
            sql,
            "INSERT INTO notifications (user_id) VALUES (%s) "
            "ON CONFLICT DO NOTHING RETURNING id",
        )
        self.assertTrue(returns_id)

        sql, returns_id = postgres_sql(
            "SELECT date(s.created_at) FROM sales s "
            "WHERE date(created_at) >= date('now', '-30 days') AND id = ?"
        )
        self.assertFalse(returns_id)
        self.assertIn("substring(s.created_at, 1, 10)::date", sql)
        self.assertIn("(CURRENT_DATE - INTERVAL '30 days')", sql)
        self.assertTrue(sql.endswith("id = %s"))

    def test_hybrid_row_supports_named_and_positional_columns(self):
        row = HybridRow((("name", "Kono"), ("id", 7)))
        self.assertEqual(row["name"], "Kono")
        self.assertEqual(row[0], "Kono")
        self.assertEqual(dict(row), {"name": "Kono", "id": 7})

    def test_postgres_row_factory_handles_commands_without_result_columns(self):
        cursor = SimpleNamespace(description=None)
        make_row = postgres_row_factory(cursor)
        self.assertIsNone(make_row(()))

    def test_postgres_row_factory_preserves_named_and_positional_query_rows(self):
        cursor = SimpleNamespace(
            description=[
                SimpleNamespace(name="name"),
                SimpleNamespace(name="id"),
            ]
        )
        row = postgres_row_factory(cursor)(("Kono Trade", 7))
        self.assertEqual(row["name"], "Kono Trade")
        self.assertEqual(row[0], "Kono Trade")
        self.assertEqual(row["id"], 7)

    def test_postgres_connection_returns_generated_id_and_converts_boolean_values(self):
        class FakeCursor:
            description = [SimpleNamespace(name="id")]
            rowcount = 1

            def fetchone(self):
                return {"id": 42}

        class FakeConnection:
            info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))

            def execute(self, statement, parameters):
                self.statement = statement
                self.parameters = parameters
                return FakeCursor()

        raw_connection = FakeConnection()
        connection = PostgresConnection(raw_connection)
        cursor = connection.execute(
            "INSERT INTO businesses (name, active) VALUES (?, ?)",
            ("Test business", True),
        )

        self.assertIn("VALUES (%s, %s) RETURNING id", raw_connection.statement)
        self.assertEqual(raw_connection.parameters, ("Test business", 1))
        self.assertEqual(cursor.lastrowid, 42)
        self.assertFalse(connection.in_transaction)

    def test_postgres_backup_connection_parses_connection_settings(self):
        url = "postgresql://backup-user@db.example.test:5432/postgres?sslmode=require"
        env = postgres_environment(url)
        self.assertEqual(env["PGHOST"], "db.example.test")
        self.assertEqual(env["PGDATABASE"], "postgres")
        self.assertEqual(env["PGUSER"], "backup-user")
        self.assertNotIn("PGPASSWORD", env)
        self.assertEqual(env["PGSSLMODE"], "require")
        self.assertEqual(postgres_database_name(url), "postgres")


if __name__ == "__main__":
    unittest.main()
