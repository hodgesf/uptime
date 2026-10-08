"""Single rolling backup of uptime.db.

There is only ever one backup, <db>.bak, replaced each time. It's written with
VACUUM INTO, so it holds just the live data (the DB file itself keeps the free
space left behind by pruning). It goes to a temp file first, so a failed or
interrupted backup never replaces the previous good one.

    python -m app.backup [path/to/uptime.db]
"""
import os
import shutil
import sqlite3
import sys

MARGIN = 50 * 1024 * 1024  # bytes of free disk to leave untouched


def backup_db(db_path: str) -> str:
    """Write <db_path>.bak and return its path. Raises if there isn't room."""
    backup_path = f"{db_path}.bak"
    tmp_path = f"{backup_path}.tmp"
    conn = sqlite3.connect(db_path)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        used_pages = conn.execute("PRAGMA page_count").fetchone()[0] - conn.execute("PRAGMA freelist_count").fetchone()[0]
        needed = used_pages * page_size + MARGIN
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
        if free < needed:
            raise RuntimeError(
                f"not enough disk space to back up {db_path}: "
                f"need ~{needed // 2**20} MB, {free // 2**20} MB free"
            )
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        try:
            conn.execute("VACUUM INTO ?", (tmp_path,))
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
    finally:
        conn.close()
    os.replace(tmp_path, backup_path)
    return backup_path


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "uptime.db"
    try:
        out = backup_db(path)
    except Exception as e:
        print(f"Backup failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Backed up to {out} ({os.path.getsize(out) // 2**20} MB)")
