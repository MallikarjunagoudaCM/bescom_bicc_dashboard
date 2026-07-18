"""
db_rollback_migration.py
------------------------
One-time migration: adds the delta_upload_log table and the upload_id
column to interruption_events if they do not already exist.

Run once against your existing bicc.db before starting the rollback-enabled app:
    python db_rollback_migration.py

Safe to run multiple times — fully idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "bicc.db"


def migrate(db_path: Path = DB) -> None:
    if not db_path.exists():
        print(f"❌  Database not found: {db_path.resolve()}")
        print("    Run db_setup.py first to create and seed bicc.db.")
        return

    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")

    try:
        with con:
            # ── 1. Upload log table ──────────────────────────────────────────
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS delta_upload_log (
                    upload_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    upload_ts   TEXT    NOT NULL,
                    filename    TEXT    NOT NULL,
                    inserted    INTEGER NOT NULL DEFAULT 0,
                    skipped     INTEGER NOT NULL DEFAULT 0,
                    status      TEXT    NOT NULL DEFAULT 'success'
                                        CHECK(status IN ('success', 'rolled_back'))
                )
                """
            )
            print("✅  delta_upload_log table ready.")

            # ── 2. Add upload_id column to interruption_events ───────────────
            try:
                con.execute(
                    "ALTER TABLE interruption_events ADD COLUMN upload_id INTEGER"
                )
                print("✅  Added upload_id column to interruption_events.")
            except sqlite3.OperationalError as exc:
                if "duplicate column" in str(exc).lower():
                    print("ℹ️   upload_id column already exists — skipped.")
                else:
                    raise

            # ── 3. Index for fast rollback deletes ───────────────────────────
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ie_upload_id
                ON interruption_events(upload_id)
                """
            )
            print("✅  Index on interruption_events(upload_id) ready.")

        print("\n✅  Migration complete. Your app is ready for rollback support.")

    finally:
        con.close()


if __name__ == "__main__":
    migrate()
