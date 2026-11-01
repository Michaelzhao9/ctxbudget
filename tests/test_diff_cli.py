"""CLI wiring for --diff, plus the receipt path that makes it usable.

Without `--disk --receipt` a user has to shell-redirect stdout to get a file to
compare later; every receipt-reading feature deserves a supported way to write
one. `--diff` takes two such files and exits 3 (like `--reclaim`) when it
refuses, so a script can tell "refused" from "nothing changed" (exit 0).
"""

import json

from ctxbudget.cli import main

HOME = "/home/lab"


def receipt(stores, projects=None, home=HOME, measured_at="2026-10-26T10:00:00Z"):
    payload = {
        "tool": "ctxbudget",
        "mode": "disk",
        "home": home,
        "measured_at": measured_at,
        "total_bytes": sum(s[1] for s in stores),
        "total_files": 4,
        "stores": [
            {"id": sid, "label": sid, "path": "/x/" + sid, "exists": True, "bytes": size,
             "files": 1, "age_hours": 1.0, "skipped_symlinks": 0}
            for sid, size in stores
        ],
        "notes": [],
    }
    if projects is not None:
        payload["projects"] = [
            {"store_id": p[0], "label": p[0], "key": p[1], "bytes": p[2], "files": 1, "share": 0.1}
            for p in projects
        ]
        payload["projects_total"] = len(projects)
        payload["projects_listed"] = len(projects)
        payload["projects_bytes"] = sum(p[2] for p in projects)
    return payload


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_diff_prints_markdown_and_exits_zero(tmp_path, capsys):
    old = write(tmp_path, "old.json", receipt([("claude", 2 * 1024**2)]))
    new = write(tmp_path, "new.json", receipt([("claude", 3 * 1024**2)]))

    code = main(["--diff", str(old), str(new)])

    out = capsys.readouterr().out
    assert code == 0
    assert "what changed between two scans" in out
    assert "+1.0 MB" in out


def test_cli_diff_json_emits_payload_with_totals(tmp_path, capsys):
    old = write(tmp_path, "old.json", receipt([("claude", 100)]))
    new = write(tmp_path, "new.json", receipt([("claude", 400)]))

    code = main(["--diff", str(old), str(new), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mode"] == "diff"
    assert payload["total_delta"] == 300


def test_cli_diff_refusal_exits_three_and_says_why(tmp_path, capsys):
    old = write(tmp_path, "old.json", receipt([("claude", 100)], home="/home/one"))
    new = write(tmp_path, "new.json", receipt([("claude", 100)], home="/home/two"))

    code = main(["--diff", str(old), str(new)])

    captured = capsys.readouterr()
    assert code == 3
    assert "refused" in captured.out.lower()
    assert "/home/one" in captured.out


def test_cli_diff_missing_file_exits_two(tmp_path, capsys):
    old = write(tmp_path, "old.json", receipt([("claude", 100)]))

    code = main(["--diff", str(old), str(tmp_path / "nope.json")])

    assert code == 2
    assert "nope.json" in capsys.readouterr().err


def test_cli_diff_rejects_a_scan_receipt(tmp_path, capsys):
    path = tmp_path / "scan.json"
    path.write_text(json.dumps({"tool": "ctxbudget", "mode": "scan"}), encoding="utf-8")

    code = main(["--diff", str(path), str(path)])

    assert code == 2
    assert "--disk" in capsys.readouterr().err


def test_cli_disk_receipt_writes_a_diffable_json_file(tmp_path, capsys):
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "proj-a").mkdir(parents=True)
    (home / ".claude" / "projects" / "proj-a" / "log.jsonl").write_bytes(b"x" * 2048)
    target = tmp_path / "receipts" / "r1.json"

    code = main(["--disk", "--home", str(home), "--by-project", "5", "--receipt", str(target)])

    written = json.loads(target.read_text(encoding="utf-8"))
    assert code == 0
    assert written["mode"] == "disk"
    assert written["projects"][0]["key"] == "proj-a"


def test_cli_disk_receipt_refuses_markdown_suffix(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()

    code = main(["--disk", "--home", str(home), "--receipt", str(tmp_path / "r.md")])

    assert code == 2
    assert "json" in capsys.readouterr().err.lower()
