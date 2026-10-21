from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

if importlib.util.find_spec("sqlalchemy") is None:
    sqlalchemy_available = False
else:
    sqlalchemy_available = True


@unittest.skipUnless(
    sqlalchemy_available, "PostgreSQL optional migration dependencies are not installed"
)
class SQLiteMigrationInventoryTests(unittest.TestCase):
    def test_inspection_counts_application_data_and_excludes_fts_internals(self):
        import sqlalchemy as sa

        from scripts.migrate_sqlite_to_postgres import (
            inspect_source,
            validate_postgres_schema,
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "source.sqlite3"
            engine = sa.create_engine(
                sa.engine.URL.create("sqlite", database=str(database))
            )
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "INSERT INTO papers(id,title) VALUES ('paper-1','A test paper')"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE paper_search_content (paper_id TEXT PRIMARY KEY, title TEXT)"
                )
                connection.exec_driver_sql(
                    "CREATE VIRTUAL TABLE paper_search USING fts5(title, content='paper_search_content')"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE)"
                )
            engine.dispose()

            report = inspect_source(database)
            schema_report = validate_postgres_schema(database)

        self.assertEqual(report["tables"]["papers"], 1)
        self.assertNotIn("paper_search_content", report["tables"])
        self.assertNotIn("paper_search", report["tables"])
        self.assertEqual(report["excluded_sqlite_virtual_tables"], ["paper_search"])
        self.assertTrue(report["excluded_sqlite_shadow_tables"])
        self.assertEqual(report["row_count"], 1)
        self.assertTrue(report["postgres_search_indexes_rebuild_required"])
        self.assertEqual(schema_report["status"], "postgresql_ddl_compiles")
        self.assertTrue(schema_report["username_case_insensitive_unique_index"])

    def test_litloom_durable_search_content_is_preserved_despite_fts_shadow_type(self):
        import sqlalchemy as sa

        from scripts.migrate_sqlite_to_postgres import inspect_source

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "source.sqlite3"
            engine = sa.create_engine(
                sa.engine.URL.create("sqlite", database=str(database))
            )
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE papers(id TEXT PRIMARY KEY, title TEXT)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE document_pages(id INTEGER PRIMARY KEY, paper_id TEXT)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE paper_search_content(rowid INTEGER PRIMARY KEY, paper_id TEXT REFERENCES papers(id), title TEXT)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE paper_page_search_content(rowid INTEGER PRIMARY KEY, page_id INTEGER REFERENCES document_pages(id), paper_id TEXT, text TEXT)"
                )
                connection.exec_driver_sql(
                    "CREATE VIRTUAL TABLE paper_search USING fts5(title, content='paper_search_content')"
                )
                connection.exec_driver_sql(
                    "CREATE VIRTUAL TABLE paper_page_search USING fts5(text, content='paper_page_search_content')"
                )
                connection.exec_driver_sql("INSERT INTO papers VALUES('p1','Paper')")
                connection.exec_driver_sql("INSERT INTO document_pages VALUES(1,'p1')")
                connection.exec_driver_sql(
                    "INSERT INTO paper_search_content VALUES(1,'p1','Paper')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO paper_page_search_content VALUES(1,1,'p1','Evidence')"
                )
            engine.dispose()

            report = inspect_source(database)

        self.assertEqual(report["tables"]["paper_search_content"], 1)
        self.assertEqual(report["tables"]["paper_page_search_content"], 1)
        self.assertNotIn("paper_search_content", report["excluded_sqlite_shadow_tables"])
        self.assertNotIn("paper_page_search_content", report["excluded_sqlite_shadow_tables"])


if __name__ == "__main__":
    unittest.main()
