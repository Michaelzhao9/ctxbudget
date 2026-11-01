"""CLI wiring for --ratio: the window comes from you, the measurement is optional.

``--ratio`` is its own mode. It never guesses a window: the receipt's
``contextWindow`` field is a declared number that this tool does not own, and a
model name does not carry a denominator. So ``--window`` is required, and every
refusal here exits 2 instead of silently measuring nothing.
"""

import json

from ctxbudget.cli import main


def session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def boundary(pre, post):
    return {
        "type": "system",
        "compactMetadata": {"preTokens": pre, "postTokens": post, "trigger": "auto"},
    }


def test_ratio_requires_a_declared_window(tmp_path, capsys):
    code = main(["--ratio", "--per-turn", "614"])

    captured = capsys.readouterr()
    assert code == 2
    assert "--window" in captured.err


def test_ratio_rejects_a_non_positive_window(capsys):
    code = main(["--ratio", "--window", "0", "--per-turn", "614"])

    captured = capsys.readouterr()
    assert code == 2
    assert "window" in captured.err.lower()


def test_ratio_rejects_a_trigger_outside_zero_to_one(capsys):
    code = main(["--ratio", "--window", "200000", "--trigger", "1.5", "--per-turn", "1"])

    captured = capsys.readouterr()
    assert code == 2
    assert "trigger" in captured.err.lower()


def test_ratio_reads_per_turn_from_the_project_scan_when_not_given(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "AGENTS.md").write_text("a" * 400, encoding="utf-8")

    code = main([str(project), "--ratio", "--window", "200000", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mode"] == "ratio"
    assert payload["declared"]["per_turn"] > 0
    assert payload["declared"]["per_turn_source"] == "scan"


def test_ratio_uses_the_measurement_directory_for_the_effective_trigger(tmp_path, capsys):
    transcripts = tmp_path / "projects"
    session(transcripts / "p" / "s.jsonl", [boundary(40000, 5000), boundary(60000, 6000)])

    code = main(
        [
            "--ratio",
            "--window",
            "200000",
            "--trigger",
            "0.8",
            "--per-turn",
            "614",
            "--measure",
            str(transcripts),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["measured"]["median_pre"] == 50000
    assert payload["measured"]["boundaries"] == 2
    assert payload["verdict"] in {"threshold_diverges", "threshold_confirmed"}


def test_ratio_refuses_to_combine_with_the_disk_modes(tmp_path, capsys):
    code = main(["--ratio", "--window", "200000", "--per-turn", "1", "--disk"])

    captured = capsys.readouterr()
    assert code == 2
    assert "--ratio" in captured.err


def test_ratio_without_measurement_still_prints_the_markdown_verdict(capsys):
    code = main(["--ratio", "--window", "200000", "--per-turn", "614"])

    out = capsys.readouterr().out
    assert code == 0
    assert "# ctxbudget" in out
    assert "no_measurement" in out
