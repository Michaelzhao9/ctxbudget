"""``--components`` as a CLI mode: it reads its own directory and answers one question."""

import json



from ctxbudget.cli import main


def boundary():
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"trigger": "auto", "preTokens": 1000, "postTokens": 100},
    }


def transcript(tmp_path):
    records = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 90}],
            },
        },
        boundary(),
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(tmp_path)


def test_components_prints_the_attribution_for_a_transcript_directory(tmp_path, capsys):
    code = main(["--components", transcript(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "tool results" in out
    assert "characters, not tokens" in out


def test_components_json_emits_the_same_payload(capsys, tmp_path):
    code = main(["--components", transcript(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["components"]["tool_result"] == 90
    assert payload["by_tool"]["Bash"]["characters"] == 90


def test_components_is_its_own_mode_and_refuses_the_ratio_flags(tmp_path, capsys):
    code = main(["--components", transcript(tmp_path), "--window", "200000"])

    err = capsys.readouterr().err
    assert code == 2
    assert "--components is its own mode" in err


def test_components_refuses_a_directory_that_does_not_exist(tmp_path, capsys):
    code = main(["--components", str(tmp_path / "missing")])

    assert code == 2
    assert "is not a directory" in capsys.readouterr().err
