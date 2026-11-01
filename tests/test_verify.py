"""Verification tests: a reclaim must prove it kept the data, not just the bytes."""

import sqlite3

import pytest

from ctxbudget.verify import fingerprint


def make_db(path, rows=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany(
        "INSERT INTO events (body) VALUES (?)", [("x" * 10,) for _ in range(rows)]
    )
    conn.commit()
    conn.close()
    return path


def test_fingerprint_reports_integrity_and_rows_per_table(tmp_path):
    db = make_db(tmp_path / "a.db", rows=7)

    facts = fingerprint(db)

    assert facts["integrity"] == "ok"
    assert facts["tables"] == {"events": 7}
    assert facts["total_rows"] == 7
    assert facts["schema_sha256"]


def test_fingerprint_ignores_sqlite_internal_tables(tmp_path):
    db = make_db(tmp_path / "a.db", rows=3)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('hi')")
    conn.commit()
    conn.close()

    facts = fingerprint(db)

    assert set(facts["tables"]) == {"events", "notes"}


def test_fingerprint_changes_when_rows_change(tmp_path):
    db = make_db(tmp_path / "a.db", rows=4)
    before = fingerprint(db)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO events (body) VALUES ('new')")
    conn.commit()
    conn.close()

    after = fingerprint(db)

    assert after["tables"]["events"] == 5
    assert after["schema_sha256"] == before["schema_sha256"]


def test_fingerprint_changes_when_schema_changes(tmp_path):
    db = make_db(tmp_path / "a.db", rows=2)
    before = fingerprint(db)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE events ADD COLUMN extra TEXT")
    conn.commit()
    conn.close()

    after = fingerprint(db)

    assert after["schema_sha256"] != before["schema_sha256"]


def test_fingerprint_refuses_non_sqlite_file(tmp_path):
    target = tmp_path / "not-a-db.txt"
    target.write_bytes(b"plain text")

    with pytest.raises(ValueError):
        fingerprint(target)
