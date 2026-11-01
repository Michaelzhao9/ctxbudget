"""Per-item listing: which few files inside a store actually hold the bytes."""

import json
import os
import time

from ctxbudget.disk import audit_stores, largest_items


def touch(path, data, age_hours=72):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - age_hours * 3600
    os.utime(path, (past, past))
    return path


def test_largest_items_ranks_files_across_stores(tmp_path):
    home = tmp_path / "home"
    touch(home / ".claude" / "projects" / "p1" / "big.jsonl", b"a" * 4096, age_hours=5)
    touch(home / ".claude" / "projects" / "p1" / "small.jsonl", b"a" * 100, age_hours=5)
    touch(home / ".codex" / "sessions" / "2026" / "r.jsonl", b"b" * 2048, age_hours=2)

    items = largest_items(audit_stores(home), limit=2)

    assert [item.bytes for item in items] == [4096, 2048]
    assert items[0].store_id == "claude-projects"
    assert items[0].relative == "p1/big.jsonl"
    assert items[0].age_hours >= 4
    assert items[1].store_id == "codex-sessions"
    assert items[1].relative == "2026/r.jsonl"


def test_largest_items_skips_symlinks_and_respects_the_limit(tmp_path):
    home = tmp_path / "home"
    outside = touch(tmp_path / "outside.bin", b"z" * 9000)
    link = home / ".claude" / "projects" / "p1" / "linked.jsonl"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    touch(home / ".claude" / "projects" / "p1" / "real.jsonl", b"a" * 512)

    items = largest_items(audit_stores(home), limit=5)

    assert [item.relative for item in items] == ["p1/real.jsonl"]
    assert largest_items(audit_stores(home), limit=0) == []


def test_largest_items_measures_a_single_file_store(tmp_path):
    home = tmp_path / "home"
    touch(home / ".local" / "share" / "opencode" / "opencode.db", b"d" * 3000, age_hours=30)

    items = largest_items(audit_stores(home), limit=3)

    assert len(items) == 1
    assert items[0].store_id == "opencode-db"
    assert items[0].relative == "opencode.db"
    assert items[0].bytes == 3000


def _home_with_three_files(tmp_path):
    home = tmp_path / "home"
    touch(home / ".claude" / "projects" / "p1" / "big.jsonl", b"a" * 4096, age_hours=5)
    touch(home / ".claude" / "projects" / "p1" / "small.jsonl", b"a" * 100, age_hours=5)
    touch(home / ".codex" / "sessions" / "2026" / "r.jsonl", b"b" * 2048, age_hours=2)
    return home


def test_disk_payload_reports_listed_items_and_coverage(tmp_path):
    from ctxbudget.report import disk_payload

    home = _home_with_three_files(tmp_path)
    stores = audit_stores(home)

    payload = disk_payload(stores, str(home), items=largest_items(stores, limit=2))

    assert payload["total_bytes"] == 6244
    assert payload["items_listed"] == 2
    assert payload["items_bytes"] == 6144
    assert payload["items_share"] == 0.984
    assert payload["items"][0]["relative"] == "p1/big.jsonl"
    assert payload["items"][0]["store_id"] == "claude-projects"


def test_disk_payload_without_items_omits_the_key(tmp_path):
    from ctxbudget.report import disk_payload

    home = _home_with_three_files(tmp_path)

    payload = disk_payload(audit_stores(home), str(home))

    assert "items" not in payload
    assert "items_share" not in payload


def test_render_disk_markdown_lists_the_largest_files(tmp_path):
    from ctxbudget.report import disk_payload, render_disk_markdown

    home = _home_with_three_files(tmp_path)
    stores = audit_stores(home)

    out = render_disk_markdown(disk_payload(stores, str(home), items=largest_items(stores, 2)))

    assert "Largest files (top 2)" in out
    assert "`p1/big.jsonl`" in out
    assert "98%" in out
    assert "4.0 KB (4,096 B)" in out


def test_cli_disk_items_flag_lists_the_largest_files(tmp_path, capsys):
    from ctxbudget.cli import main

    code = main(["--disk", "--home", str(_home_with_three_files(tmp_path)), "--items", "2", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["items_listed"] == 2
    assert payload["items"][0]["relative"] == "p1/big.jsonl"
    assert payload["items_bytes"] == 6144


def test_cli_disk_without_items_flag_keeps_the_old_shape(tmp_path, capsys):
    from ctxbudget.cli import main

    main(["--disk", "--home", str(_home_with_three_files(tmp_path)), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert "items" not in payload
    assert payload["total_bytes"] == 6244


def test_cli_items_without_disk_is_rejected(tmp_path, capsys):
    from ctxbudget.cli import main

    code = main(["--items", "3", str(tmp_path)])

    assert code == 2
    assert "--disk" in capsys.readouterr().err
