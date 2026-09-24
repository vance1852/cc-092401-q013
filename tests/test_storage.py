from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from robot_trials.storage import connect, initialize, inspect_schema, transaction


class StorageTests(unittest.TestCase):
    def test_initialize_is_repeatable(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            initialize(connection)
            initialize(connection)
            summary = inspect_schema(connection)
        finally:
            connection.close()
        self.assertEqual(summary["missing_tables"], [])
        self.assertEqual(summary["schema_version"], "3")

    def test_transaction_rolls_back_on_error(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.execute("CREATE TABLE items(value TEXT NOT NULL)")
        with self.assertRaises(RuntimeError):
            with transaction(connection):
                connection.execute("INSERT INTO items(value) VALUES('x')")
                raise RuntimeError("stop")
        count = connection.execute("SELECT count(*) FROM items").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_connect_enables_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "test.sqlite3")
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                connection.close()

    def test_v2_database_migrates_to_current_schema(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(
                """
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta VALUES('schema_version','2');
                CREATE TABLE batches (
                    batch_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL,
                    protocol_version INTEGER NOT NULL, build_id TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, sealed_at TEXT);
                CREATE TABLE observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL, source_batch TEXT NOT NULL, source_row TEXT NOT NULL,
                    robot_id TEXT NOT NULL, stratum_key TEXT NOT NULL, observed_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    imported_by TEXT NOT NULL, imported_at TEXT NOT NULL,
                    UNIQUE (batch_id, source_batch, source_row));
                INSERT INTO batches VALUES('b1','p',1,'x','running',1,'u',
                    '2026-09-21T00:00:00Z','2026-09-21T00:00:00Z',NULL);
                INSERT INTO observations(batch_id,source_batch,source_row,robot_id,stratum_key,
                    observed_at,metrics_json,content_sha256,imported_by,imported_at)
                VALUES('b1','s','r1','rb','st','2026-09-21T01:00:00Z','{}','aaaaaaaa','u',
                    '2026-09-21T01:00:00Z');
                """
            )
            initialize(connection)
            initialize(connection)
            summary = inspect_schema(connection)
            self.assertEqual(summary["missing_tables"], [])
            self.assertEqual(summary["schema_version"], "3")
            row = connection.execute("SELECT * FROM observations").fetchone()
            self.assertEqual(row["time_classification"], "normal")
            self.assertEqual(row["observed_at"], "2026-09-21T01:00:00Z")
            batch = connection.execute("SELECT * FROM batches").fetchone()
            self.assertEqual(batch["late_grace_seconds"], 86400)
            self.assertEqual(batch["future_tolerance_seconds"], 300)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
