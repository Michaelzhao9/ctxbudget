"""--soft-delete wiring: flag, payload and report.

The point of the report is the one sentence can be repeated to a vendor: rows
that the product calls deleted are still on disk, and VACUUM cannot reach them.
"""

import json
import os
import sqlite3
import time

from ctxbudget.cli import main


def make_home(tmp_path):
    home = tmp_path / "home"
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY, body TEXT, deleted_at TEXT)")
        conn.executemany(
            "INSERT INTO threads (body, deleted_at) VALUES (?, ?)",
            [("live", None), ("gone-a", "2026-10-01"), ("gone-b", "2026-10-02")],
        )
        conn.commit()
    finally:
        conn.close()
    past = time.time() - 72 * 3600
    os.utime(db, (past, past))
    return home


def test_soft_delete_requires_disk(capsys):
    assert main(["--soft-delete"]) == 2
    assert "--soft-delete requires --disk" in capsys.readouterr().err


def test_json_payload_carries_rows_bytes_and_scan_counts(tmp_path, capsys):
    home = make_home(tmp_path)

    assert main(["--disk", "--soft-delete", "--home", str(home), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["soft_delete_databases"] == 1
    assert payload["soft_delete_flagged_rows"] == 2
    assert payload["soft_delete_bytes"] == 2 * (len("gone-a") + len("2026-10-01"))
    assert [
        (f["store_id"], f["table"], f["column"], f["flagged_rows"], f["total_rows"])
        for f in payload["soft_delete"]
    ] == [("opencode-db", "threads", "deleted_at", 2, 3)]


def test_redact_hides_database_directories_in_soft_delete_findings(tmp_path, capsys):
    home = make_home(tmp_path)
    db = home / ".codex" / "private-client" / "state.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE threads (id INTEGER, deleted_at TEXT)")
        conn.execute("INSERT INTO threads VALUES (1, '2026-10-01')")
        conn.commit()
    finally:
        conn.close()

    assert main(
        ["--disk", "--soft-delete", "--redact", "--home", str(home), "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    serialized = json.dumps(payload)
    assert "private-client" not in serialized
    assert any(finding["database"].endswith("/state.sqlite") for finding in payload["soft_delete"])


def test_markdown_states_that_vacuum_cannot_reclaim_these_rows(tmp_path, capsys):
    home = make_home(tmp_path)

    assert main(["--disk", "--soft-delete", "--home", str(home)]) == 0
    out = capsys.readouterr().out

    assert "Rows marked deleted but still stored" in out
    assert "2 of 3 rows" in out
    assert "threads" in out
    assert "VACUUM cannot reclaim them" in out


def test_zero_findings_is_reported_as_scanned_not_as_empty(tmp_path, capsys):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude" / "projects" / "s.jsonl").write_text("{}\n")

    assert main(["--disk", "--soft-delete", "--home", str(home)]) == 0
    out = capsys.readouterr().out

    assert "no SQLite store found" in out


def test_scanned_databases_with_no_marker_say_so_and_cannot_be_confused_with_none(
    tmp_path, capsys
):
    home = tmp_path / "home"
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT)")
        conn.commit()
    finally:
        conn.close()

    assert main(["--disk", "--soft-delete", "--home", str(home), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["soft_delete"] == []
    assert payload["soft_delete_databases"] == 1
    assert payload["soft_delete_tables_scanned"] == 1
