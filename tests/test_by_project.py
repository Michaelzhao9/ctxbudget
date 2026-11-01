"""--by-project: which directory actually holds the bytes.

A flat file list cannot show concentration by project, so these tests use a
fully synthetic tree and aggregate bytes per top-level store directory.
"""

import json
import os
import time

from ctxbudget.disk import audit_stores, largest_items, rollup_by_project
from ctxbudget.report import disk_payload, render_disk_markdown


def touch(path, data, age_hours=72):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - age_hours * 3600
    os.utime(path, (past, past))
    return path


def home_with_two_projects(tmp_path):
    home = tmp_path / "home"
    alpha = home / ".claude" / "projects" / "-Users-demo-Projects-alpha"
    touch(alpha / "big.jsonl", b"a" * 4096, age_hours=5)
    touch(alpha / "medium.jsonl", b"a" * 2048, age_hours=5)
    beta = home / ".claude" / "projects" / "-Users-demo-Projects-beta"
    touch(beta / "small.jsonl", b"a" * 1024, age_hours=30)
    touch(home / ".codex" / "sessions" / "2026" / "10" / "rollout-x.jsonl", b"b" * 512)
    return home


def test_rollup_groups_files_by_top_level_directory_largest_first(tmp_path):
    home = home_with_two_projects(tmp_path)
    stores = audit_stores(home)

    buckets = rollup_by_project(stores)

    assert [
        (b.store_id, b.key, b.bytes, b.files) for b in buckets
    ] == [
        ("claude-projects", "-Users-demo-Projects-alpha", 6144, 2),
        ("claude-projects", "-Users-demo-Projects-beta", 1024, 1),
        ("codex-sessions", "2026", 512, 1),
    ]


def test_rollup_limit_keeps_the_largest_groups(tmp_path):
    home = home_with_two_projects(tmp_path)
    stores = audit_stores(home)

    buckets = rollup_by_project(stores, limit=1)

    assert [b.key for b in buckets] == ["-Users-demo-Projects-alpha"]


def test_rollup_treats_a_single_file_store_as_its_own_group(tmp_path):
    home = home_with_two_projects(tmp_path)
    touch(home / ".local" / "share" / "opencode" / "opencode.db", b"c" * 1024)
    stores = audit_stores(home)

    buckets = rollup_by_project(stores)

    single = [b for b in buckets if b.store_id == "opencode-db"]
    assert len(single) == 1
    assert single[0].key == "."
    assert single[0].bytes == 1024
    assert single[0].files == 1


def test_payload_reports_share_of_total_and_how_many_groups_exist(tmp_path):
    home = home_with_two_projects(tmp_path)
    stores = audit_stores(home)
    buckets = rollup_by_project(stores)

    payload = disk_payload(
        stores, str(home), projects=buckets[:1], projects_total=len(buckets)
    )

    assert payload["projects_total"] == 3
    assert payload["projects_listed"] == 1
    assert payload["projects"][0]["bytes"] == 6144
    assert payload["projects"][0]["share"] == 0.8
    assert payload["projects_share"] == 0.8  # 6144 of 7680 bytes


def test_redact_gives_a_group_the_same_ordinal_as_its_files(tmp_path):
    home = home_with_two_projects(tmp_path)
    stores = audit_stores(home)
    buckets = rollup_by_project(stores)

    payload = disk_payload(
        stores,
        str(home),
        items=largest_items(stores, 2),
        projects=buckets,
        projects_total=len(buckets),
        redact=True,
    )

    # p1 is the alpha directory in both lists, so the two tables agree.
    assert payload["items"][0]["relative"] == "p1/big.jsonl"
    assert payload["projects"][0]["key"] == "p1"
    assert payload["projects"][1]["key"] == "p2"
    assert "alpha" not in json.dumps(payload)
    assert "demo" not in json.dumps(payload)


def test_markdown_states_the_concentration_in_one_line(tmp_path):
    home = home_with_two_projects(tmp_path)
    stores = audit_stores(home)
    buckets = rollup_by_project(stores)

    out = render_disk_markdown(
        disk_payload(stores, str(home), projects=buckets[:1], projects_total=3)
    )

    assert "## Largest top-level directories (top 1)" in out
    assert "1 of 3 directories holds 80%" in out
    assert "-Users-demo-Projects-alpha" in out


def test_cli_by_project_requires_disk(tmp_path, capsys):
    from ctxbudget.cli import main

    code = main(["--by-project", "3", str(tmp_path)])

    assert code == 2
    assert "--disk" in capsys.readouterr().err


def test_cli_by_project_json_is_paste_safe_with_redact(tmp_path, capsys):
    from ctxbudget.cli import main

    home = home_with_two_projects(tmp_path)

    code = main(
        ["--disk", "--home", str(home), "--by-project", "2", "--redact", "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["projects_total"] == 3
    assert len(payload["projects"]) == 2
    assert str(home) not in json.dumps(payload)
    assert "alpha" not in json.dumps(payload)
