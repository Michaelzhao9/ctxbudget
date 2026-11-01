"""How long does the vendor keep the bytes this report is measuring?

``--disk`` prints a total and calls it the size of local agent state. That number
is only true for as long as the product keeps the files: Claude Code deletes
transcripts older than ``cleanupPeriodDays`` (default 30) at startup, and the
setting is absent from the settings file and from the app's own UI
(claude-code #105500). A byte count with no retention clause is a number with an
unstated expiry date.

Two failure modes this module must not have:

* report a schedule it did not read (a hardcoded "30" is a guess about the
  installation, not a measurement), and
* report "0 expired" for a store whose schedule is unknown — an unknown
  schedule cannot support a clean negative.

The only honest cross-check is the store itself: if files older than the
documented window are still on disk, the schedule did not run or does not use
file age, and the report has to say so.
"""

import json
import os
import time

from ctxbudget.disk import audit_stores
from ctxbudget.retention import plan_retention

DAY = 86400.0


def write(path, payload=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def age(path, days):
    when = time.time() - days * DAY
    os.utime(str(path), (when, when))


def settings(home, payload):
    write(home / ".claude" / "settings.json", json.dumps(payload).encode())


def claude_plan(tmp_path, window_days=30):
    plans, stats = plan_retention(
        audit_stores(tmp_path / "home"), tmp_path / "home", window_days=window_days
    )
    return {plan.store_id: plan for plan in plans}["claude-projects"], stats


def test_an_absent_setting_falls_back_to_the_documented_default(tmp_path):
    home = tmp_path / "home"
    settings(home, {"model": "x"})
    write(home / ".claude" / "projects" / "s.jsonl", b"a" * 100)

    plan, _ = claude_plan(tmp_path)

    assert plan.setting == "cleanupPeriodDays"
    assert plan.days == 30
    assert plan.witness == "product-default"
    assert plan.configured is False


def test_a_configured_window_overrides_the_default(tmp_path):
    home = tmp_path / "home"
    settings(home, {"cleanupPeriodDays": 7})
    write(home / ".claude" / "projects" / "s.jsonl", b"a" * 100)

    plan, _ = claude_plan(tmp_path)

    assert plan.days == 7
    assert plan.witness == "config"
    assert plan.configured is True
    assert plan.config_path.endswith("settings.json")


def test_files_older_than_the_window_are_counted_as_already_expired(tmp_path):
    home = tmp_path / "home"
    settings(home, {})
    age(write(home / ".claude" / "projects" / "old.jsonl", b"o" * 500), 40)
    write(home / ".claude" / "projects" / "new.jsonl", b"n" * 50)

    plan, stats = claude_plan(tmp_path)

    assert plan.files_expired == 1
    assert plan.bytes_expired == 500
    assert plan.verdict == "schedule_not_in_effect"
    assert stats["files_expired"] == 1
    assert stats["bytes_expired"] == 500


def test_a_store_with_no_known_schedule_claims_nothing(tmp_path):
    home = tmp_path / "home"
    age(write(home / ".codex" / "sessions" / "roll.jsonl", b"c" * 300), 400)

    plans, stats = plan_retention(audit_stores(home), home, window_days=30)
    codex = {plan.store_id: plan for plan in plans}["codex-sessions"]

    assert codex.days is None
    assert codex.witness == "unknown"
    assert codex.files_expired == 0
    assert codex.verdict == "unknown_schedule"
    assert stats["stores_unknown"] >= 1


def test_a_window_shorter_than_the_audit_window_is_flagged(tmp_path):
    home = tmp_path / "home"
    settings(home, {"cleanupPeriodDays": 7})
    write(home / ".claude" / "projects" / "s.jsonl", b"a" * 100)

    plan, _ = claude_plan(tmp_path, window_days=30)

    assert plan.shorter_than_window is True
    assert plan.verdict == "consistent"


def test_absent_stores_are_not_counted_as_measured(tmp_path):
    home = tmp_path / "home"
    settings(home, {})
    write(home / ".claude" / "projects" / "s.jsonl", b"a" * 10)

    plans, stats = plan_retention(audit_stores(home), home, window_days=30)

    absent = [plan for plan in plans if not plan.exists]
    assert absent, "known stores that do not exist should still be listed"
    assert stats["stores_measured"] == len([p for p in plans if p.exists])
    assert stats["stores_measured"] == 1


# --- report and CLI: the number must carry its expiry -----------------------

import pytest

from ctxbudget.cli import main
from ctxbudget.report import disk_payload, render_disk_markdown


def payload_for(tmp_path, redact=False, window_days=30):
    home = tmp_path / "home"
    stores = audit_stores(home)
    plans, stats = plan_retention(stores, home, window_days=window_days)
    return disk_payload(
        stores,
        home,
        redact=redact,
        retention=plans,
        retention_stats=stats,
    )


def test_markdown_total_states_how_much_is_past_its_retention_window(tmp_path):
    home = tmp_path / "home"
    settings(home, {})
    age(write(home / ".claude" / "projects" / "old.jsonl", b"o" * 5000), 40)
    write(home / ".claude" / "projects" / "new.jsonl", b"n" * 500)

    markdown = render_disk_markdown(payload_for(tmp_path))

    assert "scheduled for deletion" in markdown
    assert "4.9 KB" in markdown or "5,000 B" in markdown


def test_deletion_schedule_section_names_what_it_covered(tmp_path):
    home = tmp_path / "home"
    settings(home, {})
    write(home / ".claude" / "projects" / "s.jsonl", b"a" * 10)

    markdown = render_disk_markdown(payload_for(tmp_path))

    assert "## Deletion schedule" in markdown
    assert "coverage: 1 of 1 store(s)" in markdown
    assert "30 day(s)" in markdown
    assert "product default" in markdown
    assert "no field in the app" in markdown


def test_an_unknown_schedule_is_said_out_loud_not_left_blank(tmp_path):
    home = tmp_path / "home"
    age(write(home / ".codex" / "sessions" / "roll.jsonl", b"c" * 300), 400)

    payload = payload_for(tmp_path)
    markdown = render_disk_markdown(payload)

    assert payload["retention_stores_unknown"] == 1
    assert "coverage: 0 of 1 store(s)" in markdown
    assert "no documented deletion schedule" in markdown


def test_a_broken_schedule_is_reported_against_the_store(tmp_path):
    home = tmp_path / "home"
    settings(home, {"cleanupPeriodDays": 7})
    age(write(home / ".claude" / "projects" / "old.jsonl", b"o" * 500), 40)

    markdown = render_disk_markdown(payload_for(tmp_path))

    assert "did not remove them" in markdown
    assert "shorter than this 30 day(s) report" in markdown


def test_redact_keeps_the_config_path_out_of_the_receipt(tmp_path):
    home = tmp_path / "home"
    settings(home, {"cleanupPeriodDays": 7})
    write(home / ".claude" / "projects" / "s.jsonl", b"a" * 10)

    payload = payload_for(tmp_path, redact=True)
    entry = {row["store_id"]: row for row in payload["retention"]}["claude-projects"]

    assert entry["config_path"].endswith(".claude/settings.json")
    assert str(home) not in entry["config_path"]


def test_retention_requires_disk(tmp_path, capsys):
    assert main(["--retention"]) == 2
    assert "--retention requires --disk" in capsys.readouterr().err


def test_json_carries_the_window_and_the_plans(tmp_path, capsys):
    home = tmp_path / "home"
    settings(home, {"cleanupPeriodDays": 7})
    write(home / ".claude" / "projects" / "s.jsonl", b"a" * 10)

    code = main(["--disk", "--home", str(home), "--retention", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["retention_window_days"] == 30
    assert payload["retention_stores_known"] == 1
    assert payload["retention"][0]["witness"] == "config"
    assert payload["retention"][0]["days"] == 7
