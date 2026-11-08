"""``--images``: are the pictures in a transcript new, or the same one pasted again."""

import base64
import json
import struct


def _png(width, height, tail=b"\x00" * 16):
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    header += struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return header + tail


def _jpeg(width, height):
    sof = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08"
    sof += struct.pack(">HH", height, width) + b"\x03\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + sof + b"\xff\xd9"


def _webp(width, height):
    canvas = struct.pack("<I", 0) + struct.pack("<I", width) + struct.pack("<I", height)
    chunk = b"VP8X" + struct.pack("<I", 10) + canvas[:10]
    body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _image_block(raw, media_type):
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }


def _read_result(call_id, blocks):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": [{"type": "text", "text": "screenshot"}] + blocks,
                }
            ],
        },
    }


def _read_call(call_id, path):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": call_id, "name": "Read", "input": {"file_path": path}}
            ],
        },
    }


def _boundary():
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"trigger": "auto", "preTokens": 1000, "postTokens": 100},
    }


def _tree(tmp_path, files):
    for name, records in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(tmp_path)


def test_png_pixels_come_from_the_image_header(tmp_path):
    root = _tree(
        tmp_path := tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(_png(1200, 800), "image/png")]),
            ]
        },
    )

    from ctxbudget.images import scan_images

    payload = scan_images(root)

    assert payload["placements"] == 1
    assert payload["distinct_images"] == 1
    assert payload["pixels"] == 1200 * 800
    row = next(iter(payload["by_image"].values()))
    assert (row["width"], row["height"]) == (1200, 800)
    assert row["media_type"] == "image/png"


def test_jpeg_sof_segment_yields_width_and_height(tmp_path):
    from ctxbudget.images import scan_images

    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.jpg"),
                _read_result("r1", [_image_block(_jpeg(640, 480), "image/jpeg")]),
            ]
        },
    )

    row = next(iter(scan_images(root)["by_image"].values()))

    assert (row["width"], row["height"]) == (640, 480)
    assert row["pixels"] == 640 * 480


def test_same_picture_twice_in_one_transcript_is_a_duplicate(tmp_path):
    from ctxbudget.images import scan_images

    raw = _png(400, 300)
    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(raw, "image/png")]),
                _read_call("r2", "/shots/a.png"),
                _read_result("r2", [_image_block(raw, "image/png")]),
            ]
        },
    )

    payload = scan_images(root)

    assert payload["placements"] == 2
    assert payload["distinct_images"] == 1
    assert payload["repeat_placements"] == 1
    assert payload["same_transcript_repeat_placements"] == 1
    assert payload["cross_transcript_extra_placements"] == 0
    assert payload["pixels"] == 2 * 400 * 300
    assert payload["repeat_pixels"] == 400 * 300


def test_same_picture_in_another_transcript_is_reported_separately(tmp_path):
    from ctxbudget.images import scan_images

    raw = _png(400, 300)
    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(raw, "image/png")]),
            ],
            "b.jsonl": [
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(raw, "image/png")]),
            ],
        },
    )

    payload = scan_images(root)

    assert payload["repeat_placements"] == 1
    assert payload["same_transcript_repeat_placements"] == 0
    assert payload["cross_transcript_extra_placements"] == 1
    assert payload["shared_images"] == 1


def test_undecodable_dimensions_are_counted_not_guessed(tmp_path):
    from ctxbudget.images import scan_images

    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.webp"),
                _read_result("r1", [_image_block(_webp(800, 600), "image/webp")]),
                _read_call("r2", "/shots/b.png"),
                _read_result("r2", [_image_block(_png(10, 10), "image/png")]),
            ]
        },
    )

    payload = scan_images(root)

    assert payload["placements"] == 2
    assert payload["unknown_dimension_placements"] == 1
    assert payload["pixels"] == 100
    assert payload["distinct_images"] == 2


def test_only_images_before_a_boundary_are_charged_as_compacted(tmp_path):
    from ctxbudget.images import scan_images

    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(_png(100, 100), "image/png")]),
                _boundary(),
                _read_call("r2", "/shots/b.png"),
                _read_result("r2", [_image_block(_png(200, 200), "image/png")]),
            ]
        },
    )

    payload = scan_images(root)

    assert payload["placements"] == 2
    assert payload["pixels"] == 100 * 100 + 200 * 200
    assert payload["compacted_placements"] == 1
    assert payload["compacted_pixels"] == 100 * 100


def test_cli_images_is_its_own_mode(tmp_path):
    from ctxbudget.cli import main

    rc = main(["--images", str(tmp_path), "--disk"])
    assert rc == 2


def test_cli_images_prints_pixels_and_no_token_claim(tmp_path, capsys):
    from ctxbudget.cli import main

    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(_png(3, 2), "image/png")]),
            ]
        },
    )

    assert main(["--images", root]) == 0
    out = capsys.readouterr().out

    assert "pixels" in out
    # No invented conversion: the tool must never print a token *figure*.
    import re

    assert not re.search(r"\d[\d,.]*\s*tokens?\b", out), out


def test_an_image_pasted_into_a_prompt_is_counted_apart_from_tool_output(tmp_path):
    from ctxbudget.images import scan_images

    raw = _png(50, 40)
    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look at this"},
                            _image_block(raw, "image/png"),
                        ],
                    },
                },
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(raw, "image/png")]),
            ]
        },
    )

    payload = scan_images(root)

    assert payload["placements"] == 2
    assert payload["prompt_placements"] == 1
    assert payload["prompt_pixels"] == 50 * 40
    # the prompt copy is the first sighting, so the tool copy is the repeat
    assert payload["prompt_repeat_placements"] == 0
    assert payload["repeat_placements"] == 1
    assert payload["same_transcript_repeat_placements"] == 1


def test_api_request_blob_copies_are_skipped_and_declared(tmp_path):
    from ctxbudget.images import scan_images

    raw = _png(20, 20)
    blob = {
        "type": "api-request-blob",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": [_image_block(raw, "image/png")]}]},
    }
    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                blob,
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(raw, "image/png")]),
            ]
        },
    )

    payload = scan_images(root)

    assert payload["placements"] == 1
    assert payload["skipped_copy_placements"] == 1
    assert payload["skipped_copy_characters"] == len(_image_block(raw, "image/png")["source"]["data"])
    assert payload["repeat_placements"] == 0


def test_render_says_where_pictures_came_from(tmp_path, capsys):
    from ctxbudget.cli import main

    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                {
                    "type": "user",
                    "message": {"role": "user", "content": [_image_block(_png(8, 8), "image/png")]},
                },
                {
                    "type": "api-request-blob",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "x", "content": [_image_block(_png(8, 8), "image/png")]}],
                    },
                },
            ]
        },
    )

    assert main(["--images", root]) == 0
    out = capsys.readouterr().out

    assert "pasted into a prompt" in out
    assert "api-request-blob" in out


def test_compacted_subset_says_how_much_of_it_is_repeated(tmp_path):
    from ctxbudget.images import scan_images

    raw = _png(30, 20)
    root = _tree(
        tmp_path,
        {
            "a.jsonl": [
                _read_call("r1", "/shots/a.png"),
                _read_result("r1", [_image_block(raw, "image/png")]),
                _read_call("r2", "/shots/a.png"),
                _read_result("r2", [_image_block(raw, "image/png")]),
                _boundary(),
                _read_call("r3", "/shots/b.png"),
                _read_result("r3", [_image_block(_png(9, 9), "image/png")]),
            ]
        },
    )

    payload = scan_images(root)

    assert payload["compacted_placements"] == 2
    assert payload["compacted_pixels"] == 2 * 30 * 20
    assert payload["compacted_distinct_images"] == 1
    assert payload["compacted_repeat_placements"] == 1
    assert payload["compacted_repeat_pixels"] == 30 * 20


def _repeat_split(tmp_path, name):
    from ctxbudget.images import scan_images

    raw = _png(64, 32)
    one = [_read_call("r1", "/s/1.png"), _read_result("r1", [_image_block(raw, "image/png")])]
    two = [
        _read_call("r2", "/s/1.png"),
        _read_result("r2", [_image_block(raw, "image/png")]),
        _read_call("r3", "/s/1.png"),
        _read_result("r3", [_image_block(raw, "image/png")]),
    ]
    root = tmp_path / name
    root.mkdir()
    (root / name).write_text("\n".join(json.dumps(r) for r in one) + "\n", encoding="utf-8")
    (root / "zz-other.jsonl").write_text("\n".join(json.dumps(r) for r in two) + "\n", encoding="utf-8")
    return scan_images(str(root))


def test_the_repeat_split_does_not_depend_on_transcript_order(tmp_path):
    aaa = _repeat_split(tmp_path, "aaa.jsonl")
    mmm = _repeat_split(tmp_path, "mmm.jsonl")

    assert aaa["same_transcript_repeat_placements"] == mmm["same_transcript_repeat_placements"] == 1
    assert aaa["cross_transcript_extra_placements"] == mmm["cross_transcript_extra_placements"] == 1
    assert aaa["repeat_placements"] == mmm["repeat_placements"] == 2


def test_a_blob_counts_each_image_once_even_with_several_blocks(tmp_path):
    from ctxbudget.images import scan_images

    raw = _png(12, 12)
    blob = {
        "type": "api-request-blob",
        "message": {
            "role": "user",
            "content": [
                _image_block(raw, "image/png"),
                {"type": "tool_result", "tool_use_id": "r1", "content": [_image_block(raw, "image/png")]},
            ],
        },
    }
    root = _tree(tmp_path, {"a.jsonl": [blob]})

    payload = scan_images(root)

    assert payload["placements"] == 0
    assert payload["skipped_copy_placements"] == 2


def test_a_prompt_paste_is_a_repeat_only_inside_its_own_transcript(tmp_path):
    from ctxbudget.images import scan_images

    raw = _png(16, 16)
    paste = {
        "type": "user",
        "message": {"role": "user", "content": [_image_block(raw, "image/png")]},
    }
    root = _tree(tmp_path, {"a.jsonl": [paste], "b.jsonl": [paste]})

    payload = scan_images(root)

    assert payload["prompt_placements"] == 2
    assert payload["prompt_repeat_placements"] == 0
    assert payload["cross_transcript_extra_placements"] == 1


def test_each_transcript_is_ranked_by_the_pixels_it_holds(tmp_path):
    from ctxbudget.images import scan_images

    big = _png(800, 600)
    small = _png(100, 100)
    root = _tree(
        tmp_path,
        {
            "quiet.jsonl": [
                _read_call("r1", "/shots/small.png"),
                _read_result("r1", [_image_block(small, "image/png")]),
            ],
            "fed.jsonl": [
                _read_call("r1", "/shots/big.png"),
                _read_result("r1", [_image_block(big, "image/png")]),
                _read_call("r2", "/shots/big.png"),
                _read_result("r2", [_image_block(big, "image/png")]),
            ],
        },
    )

    payload = scan_images(root)
    rows = payload["by_transcript"]

    assert [row["transcript"] for row in rows] == ["fed.jsonl", "quiet.jsonl"]
    assert rows[0]["placements"] == 2
    assert rows[0]["distinct_images"] == 1
    assert rows[0]["pixels"] == 2 * 800 * 600
    assert rows[0]["repeat_pixels"] == 800 * 600
    assert rows[1]["pixels"] == 100 * 100
    assert sum(row["pixels"] for row in rows) == payload["pixels"]


def test_render_names_the_transcript_that_held_the_most_pixels(tmp_path, capsys):
    from ctxbudget.cli import main

    big = _png(800, 600)
    small = _png(100, 100)
    root = _tree(
        tmp_path,
        {
            "fed.jsonl": [
                _read_call("r1", "/shots/big.png"),
                _read_result("r1", [_image_block(big, "image/png")]),
            ],
            "quiet.jsonl": [
                _read_call("r1", "/shots/small.png"),
                _read_result("r1", [_image_block(small, "image/png")]),
            ],
        },
    )

    assert main(["--images", root]) == 0
    out = capsys.readouterr().out

    assert "transcripts that held the most pixels" in out
    share = "%.1f%%" % (100.0 * 800 * 600 / (800 * 600 + 100 * 100))
    assert share in out
    assert out.index("fed.jsonl") < out.index("quiet.jsonl")


def test_a_session_that_spans_files_is_one_row(tmp_path):
    from ctxbudget.images import scan_images

    big = _png(800, 600)
    small = _png(100, 100)
    session = "11111111-2222-3333-4444-555555555555"
    root = _tree(
        tmp_path,
        {
            session + ".jsonl": [
                _read_call("r1", "/shots/big.png"),
                _read_result("r1", [_image_block(big, "image/png")]),
            ],
            session + "/subagents/agent-a1.jsonl": [
                _read_call("r2", "/shots/big.png"),
                _read_result("r2", [_image_block(big, "image/png")]),
                _read_call("r3", "/shots/small.png"),
                _read_result("r3", [_image_block(small, "image/png")]),
            ],
            "other.jsonl": [
                _read_call("r4", "/shots/small.png"),
                _read_result("r4", [_image_block(small, "image/png")]),
            ],
        },
    )

    payload = scan_images(root)
    sessions = payload["by_session"]

    assert [row["session"] for row in sessions] == [session, "other.jsonl"]
    assert sessions[0]["files"] == 2
    assert sessions[0]["placements"] == 3
    assert sessions[0]["distinct_images"] == 2
    assert sessions[0]["pixels"] == 2 * 800 * 600 + 100 * 100
    assert sessions[0]["repeat_pixels"] == 800 * 600
    assert sessions[0]["subagent_files"] == 1
    assert sessions[0]["subagent_pixels"] == 800 * 600 + 100 * 100
    assert sessions[1]["files"] == 1
    assert sessions[1]["subagent_pixels"] == 0
    assert payload["sessions_with_images"] == 2
    # The per-file rows stay exactly as they were; a session row is the same
    # placements folded by conversation, so the totals must not move.
    assert len(payload["by_transcript"]) == 3
    assert sum(row["pixels"] for row in sessions) == payload["pixels"]
    assert sum(row["files"] for row in sessions) == len(payload["by_transcript"])


def test_render_names_the_session_only_when_one_session_spans_files(tmp_path, capsys):
    from ctxbudget.cli import main

    big = _png(800, 600)
    small = _png(100, 100)
    session = "11111111-2222-3333-4444-555555555555"
    split = _tree(
        tmp_path / "split",
        {
            session + ".jsonl": [
                _read_call("r1", "/shots/big.png"),
                _read_result("r1", [_image_block(big, "image/png")]),
            ],
            session + "/subagents/agent-a1.jsonl": [
                _read_call("r2", "/shots/small.png"),
                _read_result("r2", [_image_block(small, "image/png")]),
            ],
        },
    )
    assert main(["--images", split]) == 0
    out = capsys.readouterr().out

    assert "grouped by session id" in out
    assert session[:8] in out
    multi_share = "%.1f%%" % 100.0
    assert "1 session(s) hold more than one file and %s of the pixels" % multi_share in out
    subagent_share = "%.1f%%" % (100.0 * 100 * 100 / (800 * 600 + 100 * 100))
    assert (
        "%s of those pixels sit in subagent transcripts (1 file(s))" % subagent_share
        in out
    )

    plain = _tree(
        tmp_path / "plain",
        {
            "a.jsonl": [
                _read_call("r1", "/shots/big.png"),
                _read_result("r1", [_image_block(big, "image/png")]),
            ],
            "b.jsonl": [
                _read_call("r2", "/shots/small.png"),
                _read_result("r2", [_image_block(small, "image/png")]),
            ],
        },
    )
    assert main(["--images", plain]) == 0
    plain_out = capsys.readouterr().out

    assert "transcripts that held the most pixels" in plain_out
    assert "grouped by session id" not in plain_out
