"""Coverage of a --soft-delete scan: a negative answer must say what it covered.

The lab's own run-#21 thesis, turned on this tool: a check that stops early and
then says "none" is the same defect as a git hook that exits 0 without running.
``sqlite_candidates`` silently slices the candidate list at
``DEFAULT_MAX_DATABASES`` (20, largest first), and the report printed the result
as if the whole home had been read. Three facts make a zero readable:

* how many SQLite files existed at all,
* how many of them were actually opened,
* how many could not be opened.
"""

import json
import sqlite3

from ctxbudget.cli import main
from ctxbudget.disk import audit_stores, scan_soft_delete


def make_db(path, deleted=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT, deleted_at TEXT)")
        conn.execute("INSERT INTO t (body, deleted_at) VALUES ('live', NULL)")
        for index in range(deleted):
            conn.execute(
                "INSERT INTO t (body, deleted_at) VALUES (?, ?)",
                ("gone-%d" % index, "2026-10-01"),
            )
        conn.commit()
    finally:
        conn.close()


def make_home_with_databases(tmp_path, count):
    home = tmp_path / "home"
    for index in range(count):
        make_db(home / ".claude" / "projects" / ("d%d.sqlite" % index))
    return home


def test_stats_count_databases_found_before_the_cap(tmp_path):
    home = make_home_with_databases(tmp_path, 5)
    stores = audit_stores(str(home))

    _findings, stats = scan_soft_delete(stores, max_databases=2)

    assert stats["databases"] == 2, "only the capped number can be opened"
    assert stats["databases_found"] == 5, "but all five existed"
    assert stats["databases_not_scanned"] == 3


def test_markdown_says_the_scan_stopped_at_the_database_cap(tmp_path, capsys):
    home = make_home_with_databases(tmp_path, 5)
    assert main(["--disk", "--soft-delete", "--home", str(home)]) == 0
    # The default cap is 20, so force the truncation through the unit-level scan
    # and render the payload the CLI would have built.
    stores = audit_stores(str(home))
    findings, stats = scan_soft_delete(stores, max_databases=2)
    capsys.readouterr()

    from ctxbudget.report import disk_payload, render_disk_markdown

    payload = disk_payload(stores, str(home), soft_delete=findings, soft_delete_stats=stats)
    out = render_disk_markdown(payload)

    assert "coverage: opened 2 of 5 SQLite file(s)" in out
    assert "never read" in out


def test_markdown_says_when_every_file_was_opened(tmp_path, capsys):
    home = make_home_with_databases(tmp_path, 3)
    assert main(["--disk", "--soft-delete", "--home", str(home)]) == 0
    out = capsys.readouterr().out

    assert "coverage: opened every SQLite file found (3 of 3)" in out
    assert "never read" not in out


def test_json_payload_carries_the_coverage_numbers(tmp_path, capsys):
    home = make_home_with_databases(tmp_path, 4)
    stores = audit_stores(str(home))
    findings, stats = scan_soft_delete(stores, max_databases=1)

    from ctxbudget.report import disk_payload

    payload = disk_payload(stores, str(home), soft_delete=findings, soft_delete_stats=stats)

    assert payload["soft_delete_databases_found"] == 4
    assert payload["soft_delete_databases_not_scanned"] == 3
    assert payload["soft_delete_database_cap"] == 1


def test_unreadable_database_is_named_not_silently_dropped(tmp_path, capsys):
    home = make_home_with_databases(tmp_path, 2)
    broken = home / ".codex" / "broken.sqlite"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)

    assert main(["--disk", "--soft-delete", "--home", str(home), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["soft_delete_databases_skipped"] == 1

    assert main(["--disk", "--soft-delete", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "could not be opened" in out
