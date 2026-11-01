"""Disk-side tests: local agent stores grow without bound and do not shrink."""

import json
import os
import sqlite3
import time

import pytest

from ctxbudget.disk import audit_stores, reclaim_apply, reclaim_plan, sqlite_stats
from ctxbudget.report import disk_payload, render_disk_markdown


def touch(path, data, age_hours=72):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - age_hours * 3600
    os.utime(path, (past, past))
    return path


def make_db(path, rows=400, payload=2048, age_hours=72):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body BLOB)")
    conn.executemany(
        "INSERT INTO events (body) VALUES (?)", [(b"x" * payload,) for _ in range(rows)]
    )
    conn.commit()
    conn.execute("DELETE FROM events WHERE id > 20")
    conn.commit()
    conn.close()
    past = time.time() - age_hours * 3600
    os.utime(path, (past, past))
    return path


def test_audit_stores_counts_bytes_and_newest_item(tmp_path):
    home = tmp_path / "home"
    touch(home / ".claude" / "projects" / "p1" / "s1.jsonl", b"a" * 1000, age_hours=48)
    touch(home / ".codex" / "sessions" / "2026" / "r.jsonl", b"b" * 500, age_hours=2)

    result = audit_stores(home)
    by_id = {s.id: s for s in result}

    assert by_id["claude-projects"].exists is True
    assert by_id["claude-projects"].bytes == 1000
    assert by_id["claude-projects"].files == 1
    assert by_id["claude-projects"].age_hours >= 40
    assert by_id["codex-sessions"].bytes == 500
    # stores with nothing on disk are reported as missing, not silently dropped
    assert by_id["opencode-db"].exists is False
    assert by_id["opencode-db"].bytes == 0


def test_audit_does_not_follow_symlinks_out_of_the_store(tmp_path):
    home = tmp_path / "home"
    outside = touch(tmp_path / "outside.bin", b"z" * 4096)
    target = home / ".claude" / "projects" / "link"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    entry = {s.id: s for s in audit_stores(home)}["claude-projects"]

    assert entry.bytes == 0
    assert entry.skipped_symlinks == 1


def test_audit_rejects_a_top_level_store_directory_symlink(tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    touch(outside / "secret.jsonl", b"z" * 4096)
    store = home / ".claude" / "projects"
    store.parent.mkdir(parents=True)
    store.symlink_to(outside, target_is_directory=True)

    entry = {s.id: s for s in audit_stores(home)}["claude-projects"]

    assert entry.exists is False
    assert entry.bytes == 0
    assert entry.files == 0
    assert entry.skipped_symlinks == 1
    report = render_disk_markdown(disk_payload([entry], str(home)))
    assert "refused" in report.lower()
    assert "Claude Code session transcripts" in report


def test_audit_rejects_a_symlinked_ancestor_of_a_glob_store(tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "outside-codex"
    touch(outside / "state.sqlite", b"private")
    home.mkdir()
    (home / ".codex").symlink_to(outside, target_is_directory=True)

    entry = {s.id: s for s in audit_stores(home)}["codex-root-db"]

    assert entry.exists is False
    assert entry.bytes == 0
    assert entry.files == 0
    assert entry.skipped_symlinks == 1


def test_delete_does_not_shrink_a_sqlite_file(tmp_path):
    db = make_db(tmp_path / "opencode.db")
    before = db.stat().st_size

    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM events")
    conn.commit()
    conn.close()

    assert db.stat().st_size == before
    assert sqlite_stats(db)["freelist_bytes"] > 0


def test_reclaim_plan_estimates_bytes_and_is_read_only(tmp_path):
    db = make_db(tmp_path / "opencode.db")
    digest_before = db.read_bytes()

    plan = reclaim_plan(db, min_age_hours=24)

    assert plan["status"] == "planned"
    assert plan["reclaimable_bytes"] > 0
    assert plan["reclaimable_bytes"] <= plan["size_bytes"]
    assert db.read_bytes() == digest_before


def test_reclaim_refuses_a_recently_modified_database(tmp_path):
    db = make_db(tmp_path / "opencode.db", age_hours=1)

    plan = reclaim_plan(db, min_age_hours=24)

    assert plan["status"] == "refused"
    assert "modified" in plan["reason"]
    assert plan["reclaimable_bytes"] > 0


def test_reclaim_refuses_when_a_live_wal_is_present(tmp_path):
    db = make_db(tmp_path / "opencode.db")
    touch(db.parent / "opencode.db-wal", b"w" * 32, age_hours=1)

    plan = reclaim_plan(db, min_age_hours=24)

    assert plan["status"] == "refused"
    assert "wal" in plan["reason"].lower()


def test_reclaim_refuses_a_non_sqlite_file(tmp_path):
    fake = touch(tmp_path / "not-a-db", b"hello world" * 10)

    plan = reclaim_plan(fake, min_age_hours=24)

    assert plan["status"] == "refused"
    assert plan["reason"]


def test_reclaim_apply_shrinks_the_file_and_keeps_rows(tmp_path):
    db = make_db(tmp_path / "opencode.db")
    before = db.stat().st_size

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "reclaimed"
    assert db.stat().st_size < before
    assert result["after_bytes"] < before
    assert not (tmp_path / "opencode.db.vacuum-tmp").exists()
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 20
    conn.close()


def test_reclaim_apply_preserves_the_original_file_mode(tmp_path):
    db = make_db(tmp_path / "opencode.db")
    os.chmod(db, 0o600)

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "reclaimed"
    assert db.stat().st_mode & 0o777 == 0o600


def test_reclaim_apply_reports_an_equal_sized_successful_swap(tmp_path, monkeypatch):
    from ctxbudget import disk as disk_module

    db = make_db(tmp_path / "opencode.db")
    before = db.stat().st_size
    real_replace = disk_module.os.replace

    def replace_with_equal_sized_copy(source, target):
        with open(source, "ab") as handle:
            handle.truncate(before)
        real_replace(source, target)

    monkeypatch.setattr(disk_module.os, "replace", replace_with_equal_sized_copy)

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "replaced"
    assert result["after_bytes"] == before
    assert "swapped" in result["reason"]
    assert "original kept" not in result["reason"]


def test_reclaim_apply_on_already_compact_db_is_a_no_op(tmp_path):
    db = make_db(tmp_path / "opencode.db")
    first = reclaim_apply(db, min_age_hours=24)
    assert first["status"] == "reclaimed"

    os.utime(db, (time.time() - 72 * 3600,) * 2)
    second = reclaim_apply(db, min_age_hours=24)

    assert second["status"] == "no-change"
    assert second["after_bytes"] == db.stat().st_size


def test_cli_disk_json_reports_stores_and_total(tmp_path, capsys):
    home = tmp_path / "home"
    touch(home / ".claude" / "projects" / "p1" / "s.jsonl", b"a" * 2048, age_hours=6)
    from ctxbudget.cli import main

    code = main(["--disk", "--home", str(home), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mode"] == "disk"
    assert payload["total_bytes"] == 2048
    assert {s["id"] for s in payload["stores"]} >= {"claude-projects", "opencode-db"}
    assert "measured_at" in payload


def test_cli_disk_markdown_marks_missing_stores(tmp_path, capsys):
    home = tmp_path / "home"
    touch(home / ".codex" / "sessions" / "r.jsonl", b"b" * 1024, age_hours=3)
    from ctxbudget.cli import main

    code = main(["--disk", "--home", str(home)])

    out = capsys.readouterr().out
    assert code == 0
    assert "1,024" in out or "1024" in out
    assert "Not found" in out


def test_cli_reclaim_dry_run_never_writes(tmp_path, capsys):
    db = make_db(tmp_path / "opencode.db")
    before = db.read_bytes()
    from ctxbudget.cli import main

    code = main(["--reclaim", str(db), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "planned"
    assert db.read_bytes() == before


def test_cli_reclaim_apply_shrinks_and_reports_bytes(tmp_path, capsys):
    db = make_db(tmp_path / "opencode.db")
    before = db.stat().st_size
    from ctxbudget.cli import main

    code = main(["--reclaim", str(db), "--apply", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "reclaimed"
    assert payload["reclaimed_bytes"] == before - db.stat().st_size


def test_cli_refusal_exits_three(tmp_path, capsys):
    db = make_db(tmp_path / "opencode.db", age_hours=1)
    from ctxbudget.cli import main

    code = main(["--reclaim", str(db)])

    assert code == 3
    assert "refused" in capsys.readouterr().out.lower()


def test_reclaim_apply_reports_verified_row_counts(tmp_path):
    db = make_db(tmp_path / "opencode.db", rows=400)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (k TEXT)")
    conn.execute("INSERT INTO meta (k) VALUES ('v1'), ('v2')")
    conn.commit()
    conn.close()
    past = time.time() - 72 * 3600
    os.utime(db, (past, past))

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "reclaimed"
    check = result["verification"]
    assert check["ok"] is True
    assert check["issues"] == []
    assert check["rows_before"] == check["rows_after"]
    # the surviving file must really hold the rows, not just claim to
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 2
    finally:
        conn.close()


def test_reclaim_apply_fails_closed_when_file_locking_is_unavailable(tmp_path, monkeypatch):
    from ctxbudget import disk as disk_module

    db = make_db(tmp_path / "opencode.db")
    monkeypatch.setattr(disk_module, "fcntl", None)

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "refused"
    assert "lock" in result["reason"]


def test_reclaim_apply_rechecks_sidecars_before_writing(tmp_path, monkeypatch):
    from ctxbudget import disk as disk_module

    db = make_db(tmp_path / "opencode.db")
    original = db.read_bytes()
    real_fingerprint = disk_module.fingerprint
    injected = False

    def create_sidecar_after_plan(path):
        nonlocal injected
        facts = real_fingerprint(path)
        if not injected and str(path) == str(db):
            injected = True
            (tmp_path / "opencode.db-wal").write_bytes(b"live")
        return facts

    monkeypatch.setattr(disk_module, "fingerprint", create_sidecar_after_plan)

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "refused"
    assert "sidecar" in result["reason"]
    assert db.read_bytes() == original
    assert not (tmp_path / "opencode.db.vacuum-tmp").exists()


def test_reclaim_apply_rechecks_age_before_writing(tmp_path, monkeypatch):
    from ctxbudget import disk as disk_module

    db = make_db(tmp_path / "opencode.db")
    original = db.read_bytes()
    real_fingerprint = disk_module.fingerprint
    injected = False

    def touch_database_after_plan(path):
        nonlocal injected
        facts = real_fingerprint(path)
        if not injected and str(path) == str(db):
            injected = True
            os.utime(db, None)
        return facts

    monkeypatch.setattr(disk_module, "fingerprint", touch_database_after_plan)

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "refused"
    assert "safety window" in result["reason"]
    assert db.read_bytes() == original
    assert not (tmp_path / "opencode.db.vacuum-tmp").exists()


def test_reclaim_apply_keeps_the_original_when_the_copy_loses_rows(tmp_path, monkeypatch):
    from ctxbudget import disk as disk_module

    db = make_db(tmp_path / "opencode.db", rows=400)
    original_bytes = db.stat().st_size
    original_rows = 20

    real_fingerprint = disk_module.fingerprint

    def lying_fingerprint(path):
        facts = real_fingerprint(path)
        if str(path).endswith(".vacuum-tmp"):
            facts["tables"] = dict(facts["tables"], events=0)
            facts["total_rows"] = 0
        return facts

    monkeypatch.setattr(disk_module, "fingerprint", lying_fingerprint)

    result = reclaim_apply(db, min_age_hours=24)

    assert result["status"] == "refused"
    assert "row count changed for events" in result["reason"]
    assert result["verification"]["ok"] is False
    # nothing was swapped: same size, same rows, no leftover temp file
    assert db.stat().st_size == original_bytes
    assert not (tmp_path / "opencode.db.vacuum-tmp").exists()
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == original_rows
    finally:
        conn.close()
