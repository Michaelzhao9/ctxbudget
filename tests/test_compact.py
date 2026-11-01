"""What did compaction do to the window your config has to fit in?

``ctxbudget`` prices what the stack injects **per turn**. It never said how big
the window is that the injection has to fit inside, because that number is not a
configuration value — it is what the harness left after its last compaction.

Claude Code writes a receipt for every compaction into the session transcript
(``compactMetadata`` on a ``system`` record) with ``preTokens``, ``postTokens``,
``trigger`` and sometimes ``cumulativeDroppedTokens``. Nothing in the product's
own surfaces reads those fields back, so a user sees "context compacted" and no
arithmetic. This module reads the receipts and states three things per boundary:
what was there, what survived, and what the harness says it dropped.

Rules this module must keep:

1. **A ratio needs both of its numbers.** ``survived`` is ``post / pre`` and is
   ``None`` whenever either side is absent — never 0.0, never 1.0.
2. **An absent field is not a zero.** ``cumulativeDroppedTokens`` is missing on
   some boundaries; the sum covers the boundaries that carried it and reports
   how many did and did not.
3. **A negative result states what it covered.** Zero boundaries prints the file
   count it walked and refuses every ratio instead of printing ``0%``.
"""

import json

from ctxbudget.compact import scan_compactions


def boundary(pre=None, post=None, dropped=None, trigger="auto"):
    record = {"type": "system"}
    meta = {}
    if pre is not None:
        meta["preTokens"] = pre
    if post is not None:
        meta["postTokens"] = post
    if dropped is not None:
        meta["cumulativeDroppedTokens"] = dropped
    if trigger is not None:
        meta["trigger"] = trigger
    record["compactMetadata"] = meta
    return record


def write_session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def test_no_boundaries_refuses_every_ratio_and_states_coverage(tmp_path):
    payload = scan_compactions(tmp_path)

    assert payload["verdict"] == "no_compactions"
    assert payload["totals"]["boundaries"] == 0
    assert payload["survival"]["median"] is None
    assert payload["survival"]["n"] == 0
    assert payload["coverage"]["files_scanned"] == 0
    assert any("ratio" in note for note in payload["notes"])


def test_one_boundary_reports_both_numbers_and_their_ratio(tmp_path):
    write_session(
        tmp_path / "project" / "s1.jsonl",
        [{"type": "user"}, boundary(pre=100000, post=5000, dropped=95000)],
    )

    payload = scan_compactions(tmp_path)

    assert payload["verdict"] == "measured"
    assert payload["totals"]["boundaries"] == 1
    assert payload["totals"]["with_pre_and_post"] == 1
    row = payload["boundaries"][0]
    assert (row["pre"], row["post"]) == (100000, 5000)
    assert row["survived"] == 0.05
    assert row["trigger"] == "auto"
    assert payload["survival"]["median"] == 0.05
    assert payload["survival"]["n"] == 1


def test_absent_dropped_field_is_counted_not_summed_as_zero(tmp_path):
    write_session(
        tmp_path / "p" / "s1.jsonl",
        [
            boundary(pre=100000, post=5000, dropped=95000),
            boundary(pre=200000, post=8000),
        ],
    )

    payload = scan_compactions(tmp_path)

    assert payload["totals"]["dropped_sum"] == 95000
    assert payload["totals"]["dropped_present"] == 1
    assert payload["totals"]["dropped_missing"] == 1


def test_boundary_without_post_keeps_its_row_and_refuses_its_ratio(tmp_path):
    write_session(tmp_path / "p" / "s1.jsonl", [boundary(pre=100000)])

    payload = scan_compactions(tmp_path)

    assert payload["totals"]["boundaries"] == 1
    assert payload["boundaries"][0]["survived"] is None
    assert payload["totals"]["with_pre_and_post"] == 0
    assert payload["verdict"] == "no_measurable_boundaries"
    assert payload["survival"]["median"] is None


def test_malformed_lines_and_foreign_records_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "p" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write('{"type":"system","compactMetadata":"a string"}\n')
        handle.write('["a list, not an object"]\n')
        handle.write(json.dumps(boundary(pre=100000, post=10000)) + "\n")

    payload = scan_compactions(tmp_path)

    assert payload["totals"]["boundaries"] == 1
    assert payload["coverage"]["files_scanned"] == 1
    assert payload["coverage"]["files_with_boundaries"] == 1
    assert payload["survival"]["median"] == 0.1
