"""In-place schema upgrades for an existing uptime.db.

Base.metadata.create_all() creates missing tables but never alters existing
ones, so new columns on existing tables are added here. Every step is
idempotent and nothing is dropped, so history is preserved. Before changing
anything, the database is copied to uptime.db.bak-<timestamp>.

Runs automatically on startup; can also be run by hand:
    python -m app.migrations [path/to/uptime.db]
"""
import sqlite3
import sys
from datetime import datetime

# (table, column, SQL type/default) added if missing.
COLUMNS = [
    ("monitors", "health", "VARCHAR"),
    ("checks", "response_body", "VARCHAR"),
    ("state_events", "status", "VARCHAR"),
    ("query_statuses", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
]

# Fill the new columns for rows written before they existed.
BACKFILLS = [
    "UPDATE state_events SET status = CASE WHEN is_up THEN 'up' ELSE 'down' END "
    "WHERE status IS NULL",
    "UPDATE monitors SET health = CASE WHEN is_up THEN 'up' ELSE 'down' END "
    "WHERE health IS NULL AND is_up IS NOT NULL",
]


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: str) -> list[str]:
    """Bring db_path up to date. Returns the changes made (empty if none)."""
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [
            (t, c, ddl) for t, c, ddl in COLUMNS
            if t in tables and c not in _columns(conn, t)
        ]
        if not missing:
            return []

        backup_path = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        with sqlite3.connect(backup_path) as backup:
            conn.backup(backup)
        changes = [f"backed up to {backup_path}"]

        with conn:
            for t, c, ddl in missing:
                conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} {ddl}")
                changes.append(f"added {t}.{c}")
            for sql in BACKFILLS:
                conn.execute(sql)
        return changes
    finally:
        conn.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "uptime.db"
    changes = migrate(path)
    print("\n".join(changes) if changes else "Already up to date.")
