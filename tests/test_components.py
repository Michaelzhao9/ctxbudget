"""Which component actually consumed the window that got compacted?

``--postcompact`` says how much survived a compaction and ``--ratio`` says
whether the declared window matches what was measured. Neither answers the
question a user asks next: *of the 970k tokens that were just thrown away, which
part of the transcript was it?*

This module answers that from the same receipts, without a tokenizer: it walks
the content blocks that a compaction actually discarded and attributes their
characters to the component that carried them — the prompt, a system reminder,
assistant text, thinking, tool arguments, tool results (with a per-tool
breakdown), attachments, images.

Rules this module must keep:

1. **Characters, not tokens.** Nothing here guesses a tokens-per-character
   ratio; the shares are shares of counted characters and the receipt's own
   ``preTokens`` stays the only token number.
2. **No duplicate sources.** A transcript can carry the same content twice (an
   ``api-request-blob`` echoes a request whose blocks are already records); only
   the records that own the content are counted.
3. **An unattributable block is counted, not dropped.** A tool result whose
   ``tool_use_id`` was never seen still contributes its characters to the total;
   it is reported as unmapped instead of being given an invented tool name.
4. **The denominator is printed.** Shares state the character count they divide.
"""

import json
import os

from ctxbudget.components import render_components_markdown, scan_components


def boundary(pre=1000, post=100, trigger="auto"):
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"trigger": trigger, "preTokens": pre, "postTokens": post},
    }


def prompt(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def call(name, args, tool_id):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
        },
    }


def result(tool_id, content):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
        },
    }


def write_transcript(tmp_path, records):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(tmp_path)


def test_each_content_block_is_attributed_to_the_component_that_carries_it(tmp_path):
    records = [
        prompt("hello there"),
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "12345"},
                    {"type": "text", "text": "ok"},
                ],
            },
        },
        call("Bash", {"command": "ls"}, "t1"),
        result("t1", "x" * 100),
        boundary(),
    ]
    root = write_transcript(tmp_path, records)

    payload = scan_components(root)

    assert payload["components"]["prompt"] == 11
    assert payload["components"]["thinking"] == 5
    assert payload["components"]["assistant_text"] == 2
    assert payload["components"]["tool_result"] == 100
    assert payload["components"]["tool_use"] == len(json.dumps({"command": "ls"}))
    assert payload["counted_characters"] == 11 + 5 + 2 + 100 + len(json.dumps({"command": "ls"}))
    assert payload["boundaries"] == 1


def test_tool_results_are_broken_down_by_the_tool_that_produced_them(tmp_path):
    records = [
        call("Bash", {"command": "ls"}, "t1"),
        result("t1", "b" * 300),
        call("Read", {"file_path": "x"}, "t2"),
        result("t2", "r" * 100),
        boundary(),
    ]
    root = write_transcript(tmp_path, records)

    payload = scan_components(root)

    assert payload["by_tool"]["Bash"]["characters"] == 300
    assert payload["by_tool"]["Read"]["characters"] == 100
    assert payload["by_tool"]["Bash"]["share_of_tool_results"] == 0.75
    assert payload["components"]["tool_result"] == 400


def test_a_tool_result_with_no_matching_call_is_counted_but_not_named(tmp_path):
    records = [result("never-seen", "z" * 50), boundary()]
    root = write_transcript(tmp_path, records)

    payload = scan_components(root)

    assert payload["components"]["tool_result"] == 50
    assert payload["unmapped_tool_characters"] == 50
    assert payload["by_tool"] == {}


def test_a_system_reminder_is_counted_as_harness_not_as_the_users_prompt(tmp_path):
    reminder = "<system-reminder>\nbe brief\n</system-reminder>"
    records = [prompt(reminder), prompt("real question"), boundary()]
    root = write_transcript(tmp_path, records)

    payload = scan_components(root)

    assert payload["components"]["reminder"] == len(reminder)
    assert payload["components"]["prompt"] == len("real question")


def test_an_echoed_request_blob_is_not_counted_twice(tmp_path):
    records = [
        prompt("hello"),
        {
            "type": "api-request-blob",
            "message": {"content": [{"type": "text", "text": "e" * 5000}]},
        },
        boundary(),
    ]
    root = write_transcript(tmp_path, records)

    payload = scan_components(root)

    assert payload["components"]["prompt"] == 5
    assert payload["counted_characters"] == 5
    assert payload["ignored_records"] == 1


def test_a_line_that_does_not_parse_is_counted_and_the_denominator_says_so(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(prompt("hello")) + "\n" + "{not json\n" + json.dumps(boundary()) + "\n",
        encoding="utf-8",
    )

    payload = scan_components(str(tmp_path))

    assert payload["components"]["prompt"] == 5
    assert payload["unparsed_lines"] == 1


def test_content_after_the_last_boundary_is_not_attributed(tmp_path):
    records = [prompt("before"), boundary(), result("t1", "a" * 900)]
    root = write_transcript(tmp_path, records)

    payload = scan_components(root)

    assert payload["components"]["prompt"] == len("before")
    assert payload["components"]["tool_result"] == 0
    assert payload["counted_characters"] == len("before")
    assert payload["not_yet_compacted_characters"] == 900


def test_a_transcript_without_a_boundary_attributes_nothing(tmp_path):
    root = write_transcript(tmp_path, [prompt("never compacted")])

    payload = scan_components(root)

    assert payload["boundaries"] == 0
    assert payload["counted_characters"] == 0
    assert payload["components"]["prompt"] == 0
    assert payload["not_yet_compacted_characters"] == len("never compacted")


def test_the_report_names_the_largest_component_and_prints_its_denominator(tmp_path):
    records = [
        prompt("p" * 100),
        call("Bash", {"command": "ls"}, "t1"),
        result("t1", "r" * 300),
        boundary(),
    ]

    payload = scan_components(write_transcript(tmp_path, records))
    text = render_components_markdown(payload)

    share = 100.0 * payload["components"]["tool_result"] / payload["counted_characters"]
    assert "tool results" in text
    assert "%.2f%%" % share in text
    assert "{:,} characters".format(payload["counted_characters"]) in text
    assert "not tokens" in text


def test_the_report_lists_tools_in_order_with_their_share_of_tool_output(tmp_path):
    records = [
        call("Read", {"file_path": "x"}, "t2"),
        result("t2", "r" * 100),
        call("Bash", {"command": "ls"}, "t1"),
        result("t1", "b" * 300),
        boundary(),
    ]

    text = render_components_markdown(scan_components(write_transcript(tmp_path, records)))

    assert text.index("Bash") < text.index("Read")
    assert "75.00% of tool results" in text


def test_a_scan_with_no_boundary_says_so_instead_of_printing_zero_percent(tmp_path):
    root = write_transcript(tmp_path, [prompt("never compacted")])

    text = render_components_markdown(scan_components(root))

    assert "no compacted context to attribute" in text
    assert "%" not in text


def test_the_tool_list_stops_at_ten_rows_and_says_what_it_hid(tmp_path):
    records = []
    for index in range(12):
        tool_id = "t%d" % index
        records.append(call("Tool%02d" % index, {"i": index}, tool_id))
        records.append(result(tool_id, "x" * (100 - index)))
    records.append(boundary())

    text = render_components_markdown(scan_components(write_transcript(tmp_path, records)))

    assert "Tool00" in text
    assert "Tool09" in text
    assert "Tool10" not in text
    assert "2 more tools" in text
