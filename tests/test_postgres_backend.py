from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
from literature_desk.postgres import (
    Connection,
    Row,
    _engine_for,
    _translate,
    close_pools,
)


class PostgreSQLCompatibilityTests(unittest.TestCase):
    def test_qmark_conversion_ignores_quoted_values_and_comments(self):
        statement = "SELECT '?', value FROM items -- ? is a comment\nWHERE id=?"
        self.assertEqual(
            _translate(statement),
            "SELECT '?', value FROM items -- ? is a comment\nWHERE id=%s",
        )

    def test_sqlite_upsert_and_aggregate_forms_translate(self):
        self.assertEqual(
            _translate("INSERT OR IGNORE INTO items(id,value) VALUES(?,?)"),
            "INSERT INTO items(id,value) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        )
        self.assertEqual(
            _translate("SELECT SUM(status='failed'),MAX(a,b) FROM jobs WHERE title LIKE ?"),
            "SELECT SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),GREATEST(a,b) FROM jobs WHERE title ILIKE %s",
        )

    def test_rows_keep_mapping_and_positional_access(self):
        row = Row((("id", "paper-1"), ("title", "A paper")))
        self.assertEqual(row[0], "paper-1")
        self.assertEqual(row["title"], "A paper")

    def test_connection_executes_through_sqlalchemy_core_and_maps_rows(self):
        class Result:
            rowcount = 1

            def fetchone(self):
                return SimpleNamespace(
                    _mapping={"id": "paper-1", "title": "A paper"}
                )

        class CoreConnection:
            def __init__(self):
                self.calls = []

            def exec_driver_sql(self, statement, parameters):
                self.calls.append((statement, parameters))
                return Result()

        core = CoreConnection()
        connection = Connection.__new__(Connection)
        connection._connection = core
        connection._total_changes = 0

        row = connection.execute(
            "UPDATE papers SET title=? WHERE id=?", ("A paper", "paper-1")
        ).fetchone()

        self.assertEqual(
            core.calls,
            [("UPDATE papers SET title=%s WHERE id=%s", ("A paper", "paper-1"))],
        )
        self.assertEqual(row[0], "paper-1")
        self.assertEqual(row["title"], "A paper")
        self.assertEqual(connection.total_changes, 1)

    def test_close_rolls_back_and_releases_sqlalchemy_connection(self):
        events = []

        class CoreConnection:
            def in_transaction(self):
                return True

            def rollback(self):
                events.append("rollback")

            def close(self):
                events.append("close")

        connection = Connection.__new__(Connection)
        connection._connection = CoreConnection()
        connection._closed = False

        connection.close()

        self.assertEqual(events, ["rollback", "close"])
        self.assertTrue(connection._closed)

    def test_postgresql_urls_use_sqlalchemy_queue_pool_with_psycopg(self):
        with patch.dict(
            os.environ,
            {"LITLOOM_DB_POOL_MAX": "3", "LITLOOM_DB_POOL_TIMEOUT": "7"},
        ):
            engine = _engine_for("postgresql://test:test@127.0.0.1/litloom")
            try:
                self.assertEqual(engine.dialect.name, "postgresql")
                self.assertEqual(engine.dialect.driver, "psycopg")
                self.assertEqual(engine.pool.size(), 3)
                self.assertEqual(engine.pool._max_overflow, 0)
                self.assertEqual(engine.pool._timeout, 7)
            finally:
                close_pools()

    def test_runtime_refuses_to_fall_back_to_sqlite(self):
        with patch.dict(
            os.environ,
            {"LITLOOM_DATABASE_URL": "", "LITLOOM_TESTING": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "必须使用 PostgreSQL"):
                app.connect()

    def test_database_url_is_a_startup_error_when_missing(self):
        config = app.RuntimeConfig.from_env(
            {
                "LITERATURE_HOST": "127.0.0.1",
                "LITLOOM_DATABASE_URL": "",
            }
        )
        findings = config.validate(postgres_backend=False)
        database_error = next(
            item for item in findings if item["code"] == "database_url_missing"
        )
        self.assertEqual(database_error["severity"], "error")


if __name__ == "__main__":
    unittest.main()
