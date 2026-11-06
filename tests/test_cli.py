import json

import pytest

import ctxbudget

from ctxbudget.cli import main


def test_package_version_matches_release_metadata():
    assert ctxbudget.__version__ == "0.12.0"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path):
    write(tmp_path / "AGENTS.md", "# Rules\n" + "Always run the test suite. " * 30)
    write(
        tmp_path / "skills" / "pdf" / "SKILL.md",
        "---\nname: pdf\ndescription: Read and fill PDF forms when the user mentions PDFs.\n"
        "---\n\n# Steps\n" + "Open the file and inspect each field. " * 20,
    )
    write(
        tmp_path / ".mcp.json",
        json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["-y", "fs"]}}}),
    )
    dump = {
        "fs": [
            {
                "name": "read_file",
                "description": "Read a file",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]
    }
    write(tmp_path / "tools.json", json.dumps(dump))
    return tmp_path


def test_default_run_prints_a_markdown_report(project, capsys):
    code = main([str(project)])
    out = capsys.readouterr().out
    assert code == 0
    assert "per-turn" in out.lower()
    assert "AGENTS.md" in out
    assert "estimate" in out.lower()


def test_json_output_is_machine_readable_and_self_consistent(project, capsys):
    code = main([str(project), "--json", "--mcp-tools", str(project / "tools.json")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["per_turn_tokens"] == sum(
        s["tokens"] for s in payload["sources"] if s["per_turn"]
    )
    assert payload["tokenizer"]["estimated"] is True
    assert payload["root"].endswith(str(project.name))
    kinds = {s["kind"] for s in payload["sources"]}
    assert {"instruction", "skill-description", "mcp-schema"} <= kinds


def test_budget_gate_exits_one_when_over(project, capsys):
    main([str(project), "--json"])
    total = json.loads(capsys.readouterr().out)["per_turn_tokens"]

    assert main([str(project), "--max-tokens", str(total + 1)]) == 0
    assert main([str(project), "--max-tokens", str(total - 1)]) == 1
    assert "budget" in capsys.readouterr().out.lower()


def test_without_selector_reports_savings(project, capsys):
    main([str(project), "--json", "--mcp-tools", str(project / "tools.json")])
    total = json.loads(capsys.readouterr().out)["per_turn_tokens"]

    code = main(
        [
            str(project),
            "--json",
            "--mcp-tools",
            str(project / "tools.json"),
            "--without",
            "mcp:fs",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["per_turn_tokens"] < total
    assert payload["removed"][0]["selector"] == "mcp:fs"
    assert payload["removed"][0]["saved_tokens"] == total - payload["per_turn_tokens"]


def test_second_selector_reports_running_total(project, capsys):
    main([str(project), "--json", "--mcp-tools", str(project / "tools.json")])
    capsys.readouterr()
    code = main(
        [
            str(project),
            "--json",
            "--mcp-tools",
            str(project / "tools.json"),
            "--without",
            "mcp:fs",
            "--without",
            "skill:pdf",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(payload["removed"]) == 2
    # each entry's running total must equal the total after that removal
    assert payload["removed"][-1]["per_turn_tokens"] == payload["per_turn_tokens"]
    assert sum(r["saved_tokens"] for r in payload["removed"]) > 0


def test_unknown_selector_is_a_usage_error(project, capsys):
    code = main([str(project), "--without", "mcp:does-not-exist"])
    err = capsys.readouterr().err
    assert code == 2
    assert "does-not-exist" in err


def test_bad_tool_dump_path_is_a_usage_error(project, capsys):
    code = main([str(project), "--mcp-tools", str(project / "missing.json")])
    assert code == 2
    assert "missing.json" in capsys.readouterr().err


def test_missing_root_is_a_usage_error(capsys):
    assert main(["/definitely/not/here"]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_top_limits_rows_without_changing_the_total(project, capsys):
    main([str(project), "--json"])
    total = json.loads(capsys.readouterr().out)["per_turn_tokens"]
    code = main([str(project), "--top", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "%d" % total in out
    assert "more" in out.lower()


def test_duplicate_sources_are_reported(project, capsys):
    shared = "Always run the full test suite before committing and paste real output. " * 4
    write(project / ".cursorrules", shared)
    write(project / "CLAUDE.md", shared)
    code = main([str(project), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["duplicates"]
    assert payload["duplicates"][0]["similarity"] >= 0.9

    main([str(project)])
    assert "near-duplicate" in capsys.readouterr().out.lower()


def make_reclaimable_db(path, rows=400):
    import os
    import sqlite3
    import time as _time

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body BLOB)")
    conn.executemany(
        "INSERT INTO events (body) VALUES (?)", [(b"x" * 2048,) for _ in range(rows)]
    )
    conn.commit()
    conn.execute("DELETE FROM events WHERE id > 20")
    conn.commit()
    conn.close()
    past = _time.time() - 72 * 3600
    os.utime(path, (past, past))
    return path


def test_cli_reclaim_writes_a_markdown_receipt(tmp_path, capsys):
    db = make_reclaimable_db(tmp_path / "opencode.db")
    receipt = tmp_path / "receipt.md"

    code = main(["--reclaim", str(db), "--apply", "--receipt", str(receipt)])

    assert code == 0
    text = receipt.read_text(encoding="utf-8")
    assert "status: **reclaimed**" in text
    assert "rows kept: 20 -> 20" in text
    assert "integrity: ok" in text
    assert "ctxbudget " in text


def test_cli_reclaim_writes_a_json_receipt(tmp_path):
    db = make_reclaimable_db(tmp_path / "opencode.db")
    receipt = tmp_path / "receipt.json"

    code = main(["--reclaim", str(db), "--apply", "--receipt", str(receipt)])

    assert code == 0
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["mode"] == "reclaim"
    assert payload["status"] == "reclaimed"
    assert payload["verification"]["ok"] is True
    assert payload["verification"]["rows_before"] == 20
    assert payload["version"].startswith("0.")
    assert payload["generated_at"].endswith("Z")


def test_cli_receipt_records_a_refusal(tmp_path):
    import os
    import time as _time

    db = make_reclaimable_db(tmp_path / "opencode.db")
    now = _time.time()
    os.utime(db, (now, now))  # inside the safety window
    receipt = tmp_path / "refused.md"

    code = main(["--reclaim", str(db), "--receipt", str(receipt)])

    assert code == 3
    text = receipt.read_text(encoding="utf-8")
    assert "status: **refused**" in text
    assert "safety window" in text
