"""--soft-delete: rows the app marked deleted but never actually removed.

A freelist count cannot detect rows that remain allocated while carrying a
deletion marker, so these tests inspect synthetic schemas and data directly.

Read-only by construction: every query runs against ``file:...?mode=ro``.
"""

import os
import sqlite3
import time

from ctxbudget.disk import audit_stores, find_soft_delete, scan_soft_delete
from ctxbudget.report import disk_payload, render_disk_markdown


def make_home(tmp_path, name="home"):
    return tmp_path / name


def build_threads_db(path, deleted=3, live=2):
    """threads table with a deleted_at timestamp column."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE threads (id INTEGER PRIMARY KEY, title TEXT, deleted_at TEXT)"
        )
        for i in range(live):
            conn.execute(
                "INSERT INTO threads (title, deleted_at) VALUES (?, NULL)", (f"live-{i}",)
            )
        for i in range(deleted):
            conn.execute(
                "INSERT INTO threads (title, deleted_at) VALUES (?, ?)",
                (f"gone-{i}", "2026-10-01T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()
    past = time.time() - 72 * 3600
    os.utime(path, (past, past))
    return path


def test_counts_rows_with_a_deleted_at_timestamp(tmp_path):
    home = make_home(tmp_path)
    build_threads_db(home / ".claude" / "projects" / "p1" / "state.db", deleted=3, live=2)

    findings = find_soft_delete(audit_stores(home))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.table == "threads"
    assert finding.column == "deleted_at"
    assert finding.flagged_rows == 3
    assert finding.total_rows == 5
    assert finding.store_id == "claude-projects"
    assert finding.database.endswith("p1/state.db")


def test_counts_rows_with_an_integer_flag(tmp_path):
    home = make_home(tmp_path)
    path = home / ".claude" / "projects" / "p1" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY, body TEXT, is_deleted INTEGER)")
        conn.executemany(
            "INSERT INTO threads (body, is_deleted) VALUES (?, ?)",
            [("a", 0), ("b", 0), ("c", 1), ("d", 1)],
        )
        conn.commit()
    finally:
        conn.close()

    findings = find_soft_delete(audit_stores(home))

    assert [(f.column, f.flagged_rows, f.total_rows) for f in findings] == [
        ("is_deleted", 2, 4)
    ]


def test_ignores_tables_without_a_soft_delete_column(tmp_path):
    home = make_home(tmp_path)
    path = home / ".claude" / "projects" / "p1" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO events (body) VALUES (?)", [("x",), ("y",)])
        conn.commit()
    finally:
        conn.close()

    assert find_soft_delete(audit_stores(home)) == []


def test_counts_once_per_table_when_several_columns_match(tmp_path):
    home = make_home(tmp_path)
    path = home / ".claude" / "projects" / "p1" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE threads (id INTEGER PRIMARY KEY, is_deleted INTEGER, deleted_at TEXT)"
        )
        conn.execute(
            "INSERT INTO threads (is_deleted, deleted_at) VALUES (1, '2026-10-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    findings = find_soft_delete(audit_stores(home))

    # one row deleted, not two: the timestamp column wins and is the only counter
    assert [(f.column, f.flagged_rows) for f in findings] == [("deleted_at", 1)]


def test_reports_payload_bytes_of_flagged_rows(tmp_path):
    home = make_home(tmp_path)
    path = home / ".claude" / "projects" / "p1" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY, body TEXT, deleted_at TEXT)")
        conn.execute(
            "INSERT INTO threads (body, deleted_at) VALUES (?, ?)", ("x" * 500, "2026-10-01")
        )
        conn.execute("INSERT INTO threads (body, deleted_at) VALUES (?, NULL)", ("y" * 500,))
        conn.commit()
    finally:
        conn.close()

    finding = find_soft_delete(audit_stores(home))[0]

    # payload = the 500-byte body plus the 10-char timestamp
    assert finding.payload_bytes == 510


def test_leaves_the_database_untouched(tmp_path):
    home = make_home(tmp_path)
    path = build_threads_db(home / ".claude" / "projects" / "p1" / "state.db")
    before = (path.stat().st_size, path.stat().st_mtime)

    find_soft_delete(audit_stores(home))

    assert (path.stat().st_size, path.stat().st_mtime) == before


def test_skips_files_that_are_not_sqlite(tmp_path):
    home = make_home(tmp_path)
    # the claude store is JSONL in reality; a .db suffix is not proof of SQLite
    fake = home / ".claude" / "projects" / "p1" / "notes.db"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"not a database at all")
    build_threads_db(home / ".claude" / "projects" / "p1" / "state.db")

    findings = find_soft_delete(audit_stores(home))

    assert [f.database.endswith("state.db") for f in findings] == [True]


def test_counts_a_single_file_store(tmp_path):
    home = make_home(tmp_path)
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, removed INTEGER)")
        conn.executemany("INSERT INTO sessions (removed) VALUES (?)", [(0,), (1,)])
        conn.commit()
    finally:
        conn.close()

    findings = find_soft_delete(audit_stores(home))

    assert [(f.store_id, f.column, f.flagged_rows) for f in findings] == [
        ("opencode-db", "removed", 1)
    ]


def test_finds_databases_beside_the_session_stores(tmp_path):
    """Codex keeps state_*.sqlite and thread_history_*.sqlite next to sessions/."""
    home = make_home(tmp_path)
    path = home / ".codex" / "state_1.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY, removed_at TEXT)")
        conn.executemany(
            "INSERT INTO threads (removed_at) VALUES (?)", [(None,), ("2026-10-01",)]
        )
        conn.commit()
    finally:
        conn.close()

    findings = find_soft_delete(audit_stores(home))

    # Attribution moved from the generic ``codex-home`` scan root to the named
    # ``codex-root-db`` store once that store existed, so a finding joins the
    # audit row; being found twice is still prevented by the shared seen set.
    assert [(f.store_id, f.database, f.column, f.flagged_rows) for f in findings] == [
        ("codex-root-db", "state_1.sqlite", "removed_at", 1)
    ]


def test_a_database_inside_a_known_store_is_not_counted_twice(tmp_path):
    home = make_home(tmp_path)
    build_threads_db(home / ".codex" / "sessions" / "2026" / "state.db", deleted=1, live=1)

    findings = find_soft_delete(audit_stores(home))

    assert [f.store_id for f in findings] == ["codex-sessions"]


def test_row_cap_is_reported_instead_of_hiding_the_table(tmp_path):
    home = make_home(tmp_path)
    path = home / ".claude" / "projects" / "p1" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY, deleted INTEGER)")
        conn.executemany("INSERT INTO threads (deleted) VALUES (?)", [(1,)] * 40)
        conn.commit()
    finally:
        conn.close()

    findings = find_soft_delete(audit_stores(home), max_rows=10)

    assert findings[0].flagged_rows == 10
    assert findings[0].capped is True


def test_a_flag_column_with_zero_flagged_rows_is_not_reported_as_no_column(tmp_path):
    """"0 rows" and "no such column" are different findings.

    The synthetic database has a deletion-marker column whose rows are all live.
    Reporting "no deletion-marker column" would therefore overstate the result.
    """
    home = make_home(tmp_path)
    db = home / ".codex" / "sqlite" / "codex-dev.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE scan_entries (thread_id TEXT PRIMARY KEY, removed INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO scan_entries (thread_id, removed) VALUES ('t1', 0)")
        conn.commit()
    finally:
        conn.close()

    payload = scan_soft_delete_payload(home)

    assert payload["soft_delete_tables_with_flag"] == 1
    assert "a deletion flag" in render_disk_markdown(payload)


def test_summary_says_no_column_only_when_no_table_has_one(tmp_path):
    home = make_home(tmp_path)
    db = home / ".codex" / "sessions" / "a.sqlite"
    build_threads_db(db, deleted=0, live=2)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.commit()
    finally:
        conn.close()

    payload = scan_soft_delete_payload(home)

    assert payload["soft_delete_tables_with_flag"] == 1
    assert "found no deletion-marker column" not in render_disk_markdown(payload)


def scan_soft_delete_payload(home):
    stores = audit_stores(home)
    findings, stats = scan_soft_delete(stores)
    return disk_payload(
        stores,
        str(home),
        soft_delete=findings,
        soft_delete_stats=stats,
    )
