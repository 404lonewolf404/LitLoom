from __future__ import annotations

import unittest

from literature_desk.postgres import Row, _translate


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


if __name__ == "__main__":
    unittest.main()
