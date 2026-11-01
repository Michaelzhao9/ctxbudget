"""Redaction: the --items output must be safe to paste without editing.

Item paths are store-relative, but the first directory level is the client's
encoding of a project path (``-Users-...-Projects-writer``), so the raw listing
carries personal data. ``--redact`` replaces directories with per-report
ordinals and the home directory with ``~``.
"""

import json
import os
import time

from ctxbudget.disk import audit_stores, largest_items
from ctxbudget.report import disk_payload, render_disk_markdown


def touch(path, data, age_hours=72):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - age_hours * 3600
    os.utime(path, (past, past))
    return path


def home_with_named_projects(tmp_path):
    """A home whose directory names are exactly the leak we care about."""
    home = tmp_path / "home"
    secret = home / ".claude" / "projects" / "-Users-demo-Projects-client-work"
    touch(secret / "big.jsonl", b"a" * 4096, age_hours=5)
    touch(secret / "medium.jsonl", b"a" * 2048, age_hours=5)
    touch(home / ".codex" / "sessions" / "2026" / "10" / "rollout-x.jsonl", b"b" * 1024)
    return home


def test_redact_replaces_directories_with_ordinals_and_keeps_display_order(tmp_path):
    home = home_with_named_projects(tmp_path)
    stores = audit_stores(home)

    payload = disk_payload(stores, str(home), items=largest_items(stores, 5), redact=True)

    assert payload["redacted"] is True
    assert [item["relative"] for item in payload["items"]] == [
        "p1/big.jsonl",
        "p1/medium.jsonl",
        "p2/rollout-x.jsonl",
    ]
    # The ordinal is a position in this report, not a hash of the path: nothing
    # derived from the directory name survives, and equal directories share one.
    assert "demo" not in json.dumps(payload)
    assert "client-work" not in json.dumps(payload)


def test_redact_hides_the_home_directory_everywhere(tmp_path):
    home = home_with_named_projects(tmp_path)
    stores = audit_stores(home)

    payload = disk_payload(stores, str(home), items=largest_items(stores, 5), redact=True)

    assert payload["home"] == "~"
    paths = [store["path"] for store in payload["stores"]]
    assert "~/.claude/projects" in paths
    assert "~/.codex/sessions" in paths
    assert str(home) not in json.dumps(payload)
    assert any("redacted" in note for note in payload["notes"])


def test_redact_markdown_is_paste_safe(tmp_path):
    home = home_with_named_projects(tmp_path)
    stores = audit_stores(home)

    out = render_disk_markdown(
        disk_payload(stores, str(home), items=largest_items(stores, 5), redact=True)
    )

    assert str(home) not in out
    assert "demo" not in out
    assert "`p1/big.jsonl`" in out
    assert "redacted" in out.lower()


def test_without_redact_the_listing_still_shows_real_paths(tmp_path):
    home = home_with_named_projects(tmp_path)
    stores = audit_stores(home)

    payload = disk_payload(stores, str(home), items=largest_items(stores, 2))

    assert "redacted" not in payload
    assert payload["home"] == str(home)
    assert payload["items"][0]["relative"].endswith("/big.jsonl")
    assert "client-work" in payload["items"][0]["relative"]


def test_cli_redact_flag_produces_a_paste_safe_report(tmp_path, capsys):
    from ctxbudget.cli import main

    home = home_with_named_projects(tmp_path)

    code = main(["--disk", "--home", str(home), "--items", "3", "--redact", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["redacted"] is True
    assert str(home) not in json.dumps(payload)


def test_cli_redact_without_disk_is_rejected(tmp_path, capsys):
    from ctxbudget.cli import main

    code = main(["--redact", str(tmp_path)])

    assert code == 2
    assert "--disk" in capsys.readouterr().err
