"""CLI wiring for --postcompact: the receipts, and what the turn cost is inside them.

``--postcompact`` is a separate mode, not a flag on the project scan: it reads a
directory of transcripts rather than a project, and it answers a different
question (what the window became, not what the stack injects).

``--per-turn N`` is the only bridge, and it takes the number from the user
instead of inventing one: the project scan prints per-turn cost, the compaction
receipt prints the window, and the share between them is ``N / median post``
with both numbers in the sentence.
"""

import json

from ctxbudget.cli import main


def session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def boundary(pre, post, dropped=None):
    meta = {"preTokens": pre, "postTokens": post, "trigger": "auto"}
    if dropped is not None:
        meta["cumulativeDroppedTokens"] = dropped
    return {"type": "system", "compactMetadata": meta}


def test_markdown_states_coverage_and_both_numbers_per_boundary(tmp_path, capsys):
    session(
        tmp_path / "p" / "s1.jsonl",
        [boundary(200000, 10000, 190000), boundary(100000, 5000)],
    )
    session(tmp_path / "p" / "s2.jsonl", [{"type": "user", "message": "hi"}])

    code = main(["--postcompact", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "2 file(s) scanned" in out
    assert "1 with compaction receipts" in out
    assert "100,000" in out and "5,000" in out
    assert "verdict: **measured**" in out
    assert "1 boundary carries no" in out  # absent field, not a zero


def test_json_payload_names_the_mode_and_the_totals(tmp_path, capsys):
    session(tmp_path / "p" / "s1.jsonl", [boundary(100000, 5000, 95000)])

    code = main(["--postcompact", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mode"] == "postcompact"
    assert payload["totals"]["boundaries"] == 1
    assert payload["totals"]["dropped_sum"] == 95000


def test_per_turn_prints_its_numerator_and_denominator(tmp_path, capsys):
    session(tmp_path / "p" / "s1.jsonl", [boundary(100000, 5000, 95000)])

    code = main(["--postcompact", str(tmp_path), "--per-turn", "100"])

    out = capsys.readouterr().out
    assert code == 0
    assert "100 per-turn tokens" in out
    assert "5,000" in out
    assert "2.0%" in out


def test_per_turn_share_is_refused_when_nothing_was_measured(tmp_path, capsys):
    code = main(["--postcompact", str(tmp_path), "--per-turn", "100", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["verdict"] == "no_compactions"
    assert payload["turn_cost"] is None
    assert any("turn cost" in note for note in payload["notes"])


def test_missing_directory_exits_2(tmp_path, capsys):
    code = main(["--postcompact", str(tmp_path / "nope")])

    captured = capsys.readouterr()
    assert code == 2
    assert "not a directory" in captured.err


def test_deep_transcript_paths_are_shortened_in_the_table(tmp_path, capsys):
    deep = (
        tmp_path
        / "encoded-project-directory-that-is-far-too-long-for-a-table-cell"
        / "s1.jsonl"
    )
    session(deep, [boundary(100000, 5000, 95000)])

    code = main(["--postcompact", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "…" in out
    assert "encoded-project-directory-that-is-far-too-long-for-a-table-cell" not in out


def test_half_token_median_prints_as_a_whole_token(tmp_path, capsys):
    session(
        tmp_path / "p" / "s1.jsonl",
        [boundary(100000, 5000), boundary(200000, 5001)],
    )

    code = main(["--postcompact", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["window"]["median_post"] == 5000 or payload["window"]["median_post"] == 5001
    assert isinstance(payload["window"]["median_post"], int)


def test_plural_boundaries_take_plural_verbs_in_the_absent_field_line(tmp_path, capsys):
    session(
        tmp_path / "p" / "s1.jsonl",
        [
            boundary(100000, 5000, 95000),
            boundary(200000, 8000, 192000),
            boundary(150000, 6000),
            boundary(180000, 7000),
        ],
    )

    code = main(["--postcompact", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "2 boundaries carry no such field" in out
    assert "and are not counted as zero" in out
    assert "carries no such field and is not counted" in out or "2 boundaries carry" in out
