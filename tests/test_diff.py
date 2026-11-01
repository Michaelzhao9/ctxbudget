"""--diff: compare two disk receipts and rank what grew.

A single scan can never show a growth rate. claude-mem #5057 reports ~23.5 MB per
sync (~150 GB/day); MangoDisk #79 asks for exactly this: an opt-in comparison of
two scans of the same root with per-folder byte deltas sorted by growth.

The guard that matters is refusing meaningless comparisons: receipts taken from
different homes describe different machines, so a delta between them is noise
dressed as a number.
"""

import json

import pytest

from ctxbudget.diff import diff_receipts, load_receipt
from ctxbudget.report import render_diff_markdown


def receipt(home, stores, projects=None, measured_at="2026-10-26T10:00:00Z", total=None):
    total_bytes = sum(s[1] for s in stores) if total is None else total
    payload = {
        "tool": "ctxbudget",
        "mode": "disk",
        "home": str(home),
        "measured_at": measured_at,
        "total_bytes": total_bytes,
        "total_files": 10,
        "stores": [
            {
                "id": sid,
                "label": sid,
                "path": "/x/" + sid,
                "exists": True,
                "bytes": size,
                "files": 1,
                "age_hours": 1.0,
                "skipped_symlinks": 0,
            }
            for sid, size in stores
        ],
        "notes": [],
    }
    if projects is not None:
        payload["projects_total"] = len(projects)
        payload["projects_listed"] = len(projects)
        payload["projects_bytes"] = sum(p[2] for p in projects)
        payload["projects_share"] = 0.5
        payload["projects"] = [
            {
                "store_id": p[0],
                "label": p[0],
                "key": p[1],
                "bytes": p[2],
                "files": 1,
                "share": 0.1,
            }
            for p in projects
        ]
    return payload


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_receipt_returns_dict_for_disk_receipt(tmp_path):
    path = write(tmp_path, "r.json", receipt("/home/x", [("claude", 10)]))

    assert load_receipt(str(path))["mode"] == "disk"


def test_load_receipt_rejects_json_that_is_not_a_receipt(tmp_path):
    path = write(tmp_path, "r.json", {"hello": "world"})

    with pytest.raises(ValueError):
        load_receipt(str(path))


def test_load_receipt_rejects_scan_mode_payload(tmp_path):
    path = write(tmp_path, "r.json", {"tool": "ctxbudget", "mode": "scan"})

    with pytest.raises(ValueError) as excinfo:
        load_receipt(str(path))

    assert "disk" in str(excinfo.value)


def test_diff_totals_and_store_deltas(tmp_path):
    old = receipt("/home/x", [("claude", 1000), ("codex", 500)])
    new = receipt("/home/x", [("claude", 1400), ("codex", 400)])

    payload = diff_receipts(old, new)

    assert payload["status"] == "ok"
    assert payload["total_before"] == 1500
    assert payload["total_after"] == 1800
    assert payload["total_delta"] == 300
    assert [(s["store_id"], s["delta"]) for s in payload["stores"]] == [
        ("claude", 400),
        ("codex", -100),
    ]


def test_diff_refuses_receipts_from_different_homes(tmp_path):
    old = receipt("/home/a", [("claude", 1000)])
    new = receipt("/home/b", [("claude", 1000)])

    payload = diff_receipts(old, new)

    assert payload["status"] == "refused"
    assert "/home/a" in payload["reason"] and "/home/b" in payload["reason"]
    assert "stores" not in payload


def test_diff_compares_projects_when_both_receipts_have_them():
    old = receipt(
        "/home/x",
        [("claude", 3000)],
        projects=[("claude", "proj-a", 1000), ("claude", "proj-b", 2000)],
    )
    new = receipt(
        "/home/x",
        [("claude", 4000)],
        projects=[("claude", "proj-a", 500), ("claude", "proj-b", 3500)],
    )

    payload = diff_receipts(old, new)

    assert payload["projects_available"] is True
    assert [(p["key"], p["delta"]) for p in payload["projects"]] == [
        ("proj-b", 1500),
        ("proj-a", -500),
    ]


def test_diff_marks_project_absent_from_the_old_receipt_as_new(tmp_path):
    old = receipt("/home/x", [("claude", 1000)], projects=[("claude", "proj-a", 1000)])
    new = receipt(
        "/home/x",
        [("claude", 2500)],
        projects=[("claude", "proj-a", 1000), ("claude", "proj-new", 1500)],
    )

    payload = diff_receipts(old, new)

    assert [(p["key"], p["before"], p["delta"]) for p in payload["projects"]] == [
        ("proj-new", 0, 1500),
        ("proj-a", 1000, 0),
    ]


def test_diff_omits_projects_when_only_one_receipt_has_them():
    old = receipt("/home/x", [("claude", 1000)])
    new = receipt("/home/x", [("claude", 2000)], projects=[("claude", "proj-a", 2000)])

    payload = diff_receipts(old, new)

    assert payload["projects_available"] is False
    assert not payload.get("projects")
    assert "projects" in payload["projects_note"]


def test_diff_limits_project_rows_but_keeps_all_stores():
    old = receipt("/home/x", [("claude", 3000)], projects=[("claude", "p%d" % i, 100) for i in range(5)])
    new = receipt("/home/x", [("claude", 3000)], projects=[("claude", "p%d" % i, 100 + i) for i in range(5)])

    payload = diff_receipts(old, new, top=2)

    assert len(payload["projects"]) == 2
    assert payload["projects_total"] == 5
    assert len(payload["stores"]) == 1


def test_diff_reports_span_between_measurements():
    old = receipt("/home/x", [("claude", 1)], measured_at="2026-10-26T10:00:00Z")
    new = receipt("/home/x", [("claude", 2)], measured_at="2026-10-26T12:30:00Z")

    payload = diff_receipts(old, new)

    assert payload["span_hours"] == pytest.approx(2.5, abs=0.01)


def test_diff_render_shows_totals_growth_and_the_span():
    old = receipt("/home/x", [("claude", 1024**3)])
    new = receipt("/home/x", [("claude", 1024**3 + 1024**2)], measured_at="2026-10-26T12:00:00Z")

    text = render_diff_markdown(diff_receipts(old, new))

    assert "## Total" in text
    assert "+1.0 MB" in text
    assert "span: 2.0 h" in text


def test_diff_render_refusal_explains_itself():
    payload = diff_receipts(receipt("/home/a", [("c", 1)]), receipt("/home/b", [("c", 1)]))

    text = render_diff_markdown(payload)

    assert "refused" in text.lower()
    assert "/home/a" in text


def test_diff_render_does_not_claim_a_zero_span_is_zero_elapsed():
    old = receipt("/home/x", [("claude", 10)], measured_at="2026-10-26T10:00:00Z")
    new = receipt("/home/x", [("claude", 20)], measured_at="2026-10-26T10:00:00Z")

    text = render_diff_markdown(diff_receipts(old, new))

    assert "span: 0 min" not in text
    assert "under 1 min" in text


def test_diff_project_rows_name_the_store_with_its_label():
    old = receipt("/home/x", [("claude-projects", 10)], projects=[("claude-projects", "p", 10)])
    new = receipt("/home/x", [("claude-projects", 20)], projects=[("claude-projects", "p", 20)])
    new["stores"][0]["label"] = "Claude Code session transcripts"

    text = render_diff_markdown(diff_receipts(old, new))

    assert "Claude Code session transcripts" in text.split("directories by growth")[1]
    assert "| `p` | claude-projects |" not in text


def test_redacted_receipts_do_not_claim_the_same_machine_check_ran():
    """Redaction rewrites the home to ``~`` on both sides, so equality is not evidence.

    A redacted receipt stays useful for a before/after on one machine, but the
    cross-machine guard cannot fire: the payload must say so instead of implying
    the two receipts were proven to come from the same tree.
    """
    old = receipt("~", [("claude-projects", 1000)], measured_at="2026-10-26T10:00:00Z")
    new = receipt("~", [("claude-projects", 4000)], measured_at="2026-10-26T22:00:00Z")
    old["redacted"] = True
    new["redacted"] = True

    payload = diff_receipts(old, new)

    assert payload["status"] == "ok"
    assert payload["home_verified"] is False
    assert any("redacted" in note for note in payload["notes"])
    markdown = render_diff_markdown(payload)
    assert "home check: **not run**" in markdown
    assert "redacted" in markdown
