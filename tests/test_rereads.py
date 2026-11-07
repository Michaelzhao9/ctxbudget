"""``--rereads``: how much of the compacted context was the same file read again."""

import json


def _read_call(call_id, path, offset=None, limit=None):
    payload = {"file_path": path}
    if offset is not None:
        payload["offset"] = offset
    if limit is not None:
        payload["limit"] = limit
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": "Read", "input": payload}],
        },
    }


def _result(call_id, characters):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "x" * characters}],
        },
    }


def _boundary():
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"trigger": "auto", "preTokens": 1000, "postTokens": 100},
    }


def _tree(tmp_path, records, name="s.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(tmp_path)


def test_same_file_read_twice_charges_the_second_read_as_repeat(tmp_path):
    from ctxbudget.rereads import scan_rereads

    root = _tree(
        tmp_path,
        [
            _read_call("r1", "/repo/a.py"),
            _result("r1", 100),
            _read_call("r2", "/repo/a.py"),
            _result("r2", 40),
            _boundary(),
        ],
    )

    payload = scan_rereads(root)

    row = payload["by_path"]["/repo/a.py"]
    assert row["reads"] == 2
    assert row["characters"] == 140
    assert row["repeat_characters"] == 40
    assert payload["read_characters"] == 140
    assert payload["repeat_characters"] == 40


def test_the_same_range_read_twice_is_charged_as_a_true_duplicate(tmp_path):
    from ctxbudget.rereads import scan_rereads

    root = _tree(
        tmp_path,
        [
            _read_call("r1", "/repo/b.py", offset=10, limit=20),
            _result("r1", 100),
            _read_call("r2", "/repo/b.py", offset=30, limit=20),
            _result("r2", 70),
            _read_call("r3", "/repo/b.py", offset=10, limit=20),
            _result("r3", 100),
            _boundary(),
        ],
    )

    payload = scan_rereads(root)

    row = payload["by_path"]["/repo/b.py"]
    assert row["reads"] == 3
    assert row["repeat_characters"] == 170
    assert row["identical_repeats"] == 1
    assert payload["identical_repeat_characters"] == 100


def test_a_result_whose_call_is_missing_is_counted_but_no_path_is_invented(tmp_path):
    from ctxbudget.rereads import scan_rereads

    root = _tree(
        tmp_path,
        [
            _result("ghost", 50),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls"}}
                    ],
                },
            },
            _result("b1", 30),
            _read_call("r1", "/repo/c.py"),
            _result("r1", 10),
            _boundary(),
        ],
    )

    payload = scan_rereads(root)

    assert payload["unmapped_read_characters"] == 50
    assert payload["read_characters"] == 10
    assert list(payload["by_path"]) == ["/repo/c.py"]


def test_content_after_the_last_boundary_is_reported_not_charged(tmp_path):
    from ctxbudget.rereads import scan_rereads

    root = _tree(
        tmp_path,
        [
            _read_call("r1", "/repo/d.py"),
            _result("r1", 10),
            _boundary(),
            _read_call("r2", "/repo/d.py"),
            _result("r2", 999),
        ],
    )

    payload = scan_rereads(root)

    assert payload["read_characters"] == 10
    assert payload["not_yet_compacted_read_characters"] == 999
    assert payload["by_path"]["/repo/d.py"]["reads"] == 1


def test_rereads_is_its_own_mode_and_prints_the_repeat_share(tmp_path, capsys):
    from ctxbudget.cli import main

    root = _tree(
        tmp_path,
        [
            _read_call("r1", "/repo/e.py"),
            _result("r1", 100),
            _read_call("r2", "/repo/e.py"),
            _result("r2", 40),
            _boundary(),
        ],
    )

    code = main(["--rereads", root])

    out = capsys.readouterr().out
    assert code == 0
    assert "/repo/e.py" in out
    assert "140" in out
    assert "28.57%" in out
    assert "characters, not tokens" in out


def test_the_headline_counts_paths_and_reads_as_two_different_populations(tmp_path, capsys):
    from ctxbudget.cli import main

    root = _tree(
        tmp_path,
        [
            _read_call("r1", "/repo/f.py"),
            _result("r1", 10),
            _read_call("r2", "/repo/f.py"),
            _result("r2", 20),
            _read_call("r3", "/repo/g.py"),
            _result("r3", 5),
            _boundary(),
        ],
    )

    code = main(["--rereads", root])
    out = capsys.readouterr().out

    assert code == 0
    assert "1 paths were read more than once out of 2 paths (3 reads)" in out


def test_a_duplicate_after_the_last_boundary_is_not_charged_as_a_duplicate(tmp_path):
    from ctxbudget.rereads import scan_rereads

    root = _tree(
        tmp_path,
        [
            _read_call("r1", "/repo/h.py"),
            _result("r1", 10),
            _boundary(),
            _read_call("r2", "/repo/h.py"),
            _result("r2", 50),
            _read_call("r3", "/repo/h.py"),
            _result("r3", 50),
        ],
    )

    payload = scan_rereads(root)

    assert payload["read_characters"] == 10
    assert payload["repeat_characters"] == 0
    assert payload["identical_repeat_characters"] == 0


def test_a_path_only_seen_after_a_boundary_elsewhere_does_not_make_a_charged_read_a_repeat(tmp_path):
    from ctxbudget.rereads import scan_rereads

    _tree(
        tmp_path,
        [_read_call("r1", "/repo/i.py"), _result("r1", 500)],
        name="a.jsonl",
    )
    _tree(
        tmp_path,
        [_read_call("r2", "/repo/i.py"), _result("r2", 30), _boundary()],
        name="b.jsonl",
    )

    payload = scan_rereads(str(tmp_path))

    assert payload["read_characters"] == 30
    assert payload["repeat_characters"] == 0


def _image_result(call_id, base64_chars):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": [
                        {"type": "image", "source": {"type": "base64", "data": "A" * base64_chars}}
                    ],
                }
            ],
        },
    }


def test_image_reads_are_counted_apart_because_base64_is_a_bad_token_proxy(tmp_path):
    from ctxbudget.rereads import scan_rereads

    root = _tree(
        tmp_path,
        [
            _read_call("r1", "/repo/shot.png"),
            _image_result("r1", 200),
            _read_call("r2", "/repo/code.py"),
            _result("r2", 80),
            _boundary(),
        ],
    )

    payload = scan_rereads(root)

    assert payload["by_path"]["/repo/shot.png"]["kind"] == "image"
    assert payload["by_path"]["/repo/code.py"]["kind"] == "text"
    assert payload["image_read_characters"] == payload["by_path"]["/repo/shot.png"]["characters"]
    assert payload["image_read_characters"] > 200
    assert payload["text_read_characters"] == 80
