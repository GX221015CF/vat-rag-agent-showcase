from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tax_rag.storage.runtime_store import RuntimeStore


class P2BSubmissionTokenMigrationTests(unittest.TestCase):
    def test_existing_runtime_database_adds_token_column_and_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "runtime.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE matters (
                    matter_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    round_count INTEGER NOT NULL,
                    notice_version TEXT NOT NULL,
                    notice_accepted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE messages (
                    message_id TEXT PRIMARY KEY,
                    matter_id TEXT NOT NULL REFERENCES matters(matter_id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    round_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO matters(
                    matter_id,status,round_count,notice_version,notice_accepted_at,
                    created_at,updated_at,closed_at
                ) VALUES(
                    'legacy-matter','ACTIVE',1,'cloud-transfer-v2','2026-08-31T00:00:00+00:00',
                    '2026-08-31T00:00:00+00:00','2026-08-31T00:00:00+00:00',NULL
                );
                INSERT INTO messages(message_id,matter_id,role,round_number,content,created_at)
                VALUES(
                    'legacy-message','legacy-matter','USER',1,'旧消息',
                    '2026-08-31T00:00:00+00:00'
                );
                """
            )
            connection.close()

            project_root = Path(__file__).resolve().parents[1]
            store = RuntimeStore(
                database_path,
                project_root / "src" / "tax_rag" / "storage" / "runtime_schema.sql",
            )
            migrated = store.connect()
            try:
                columns = {
                    row["name"]
                    for row in migrated.execute("PRAGMA table_info(messages)").fetchall()
                }
                indexes = {
                    row["name"]
                    for row in migrated.execute("PRAGMA index_list(messages)").fetchall()
                }
                message = migrated.execute(
                    "SELECT content,submission_token FROM messages WHERE message_id='legacy-message'"
                ).fetchone()
            finally:
                migrated.close()

            self.assertIn("submission_token", columns)
            self.assertIn("idx_messages_matter_submission_token", indexes)
            self.assertEqual(message["content"], "旧消息")
            self.assertIsNone(message["submission_token"])


if __name__ == "__main__":
    unittest.main()
