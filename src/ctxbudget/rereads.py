"""Charge the characters spent reading a file more than once.

A compaction discards whatever text sat before it. Splitting that text by tool name
(``--components``) showed ``Read`` carries most of it. The next question is whether the
cost is *large files* or *the same file read again*: those two call for different fixes
(read narrower ranges vs. keep what you already read).
"""

import json
import os

CONTENT_RECORD_TYPES = ("user", "assistant")


def _text_length(value):
    return len(value) if isinstance(value, str) else 0


def _result_characters(block):
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False, sort_keys=True))


def _kind_of(block):
    """Image reads carry base64, whose length is not a proxy for tokens at all."""
    content = block.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("image", "document"):
                return "image"
    return "text"


def _content_blocks(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _empty_segment():
    """Reads collected since the last boundary, plus what could not be attributed."""
    return {"requests": [], "unmapped": 0}


def _new_row():
    return {
        "reads": 0,
        "characters": 0,
        "repeat_characters": 0,
        "identical_repeats": 0,
        "identical_repeat_characters": 0,
    }


def _settle(totals, segment, seen_paths, seen_requests, acc):
    """Book a segment only once a boundary has shown it was thrown away.

    The seen-sets and the duplicate counters move here, not to parse time: content
    after the last boundary is still in context, so a read in it must not be able to
    make an earlier, genuinely compacted read look like a repeat.
    """
    for path, offset, limit, characters, kind in segment["requests"]:
        row = totals.setdefault(path, _new_row())
        row["kind"] = kind if kind == "image" else row.get("kind", "text")
        row["reads"] += 1
        row["characters"] += characters
        is_repeat = path in seen_paths
        if is_repeat:
            row["repeat_characters"] += characters
            acc["repeat_characters"] += characters
        else:
            seen_paths.add(path)
        # A re-read of the *same range* is a true duplicate; a re-read at a
        # different offset is reading new lines.
        request = (path, offset, limit)
        if request in seen_requests:
            row["identical_repeats"] += 1
            row["identical_repeat_characters"] += characters
            acc["identical_repeat_characters"] += characters
        else:
            seen_requests.add(request)
        if kind == "image":
            acc["image_read_characters"] += characters
            if is_repeat:
                acc["image_repeat_characters"] += characters
    acc["unmapped_read_characters"] += segment["unmapped"]


def scan_rereads(root):
    """Attribute every compacted ``Read`` result to the path it returned.

    The first charged read of a path is the cost of learning it; every later read of
    the same path is charged as a repeat.
    """
    root = os.path.abspath(os.path.expanduser(str(root)))
    totals = {}
    seen_paths = set()
    seen_requests = set()
    acc = {
        "repeat_characters": 0,
        "identical_repeat_characters": 0,
        "unmapped_read_characters": 0,
        "image_read_characters": 0,
        "image_repeat_characters": 0,
    }
    not_yet_compacted_read_characters = 0
    boundaries = 0
    files_scanned = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(current, name)
            if os.path.islink(full):
                continue
            files_scanned += 1
            tool_calls = {}
            segment = _empty_segment()
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        if isinstance(record.get("compactMetadata"), dict):
                            _settle(totals, segment, seen_paths, seen_requests, acc)
                            segment = _empty_segment()
                            boundaries += 1
                            continue
                        if record.get("type") not in CONTENT_RECORD_TYPES:
                            continue
                        for block in _content_blocks(record):
                            if block.get("type") == "tool_use":
                                tool_calls[block.get("id")] = block
                            elif block.get("type") == "tool_result":
                                call = tool_calls.get(block.get("tool_use_id"))
                                if call is None:
                                    # The call may sit in a part of the transcript that
                                    # was already compacted away. Charge the characters,
                                    # but never invent the path they came from.
                                    if block.get("tool_use_id") is not None:
                                        segment["unmapped"] += _result_characters(block)
                                    continue
                                if call.get("name") != "Read":
                                    continue
                                args = call.get("input")
                                if not isinstance(args, dict):
                                    continue
                                path_value = args.get("file_path")
                                if not isinstance(path_value, str) or not path_value:
                                    continue
                                segment["requests"].append(
                                    (
                                        path_value,
                                        args.get("offset"),
                                        args.get("limit"),
                                        _result_characters(block),
                                        _kind_of(block),
                                    )
                                )
            except OSError:
                continue
            # Reads after the last boundary are still in context: reported, not charged.
            not_yet_compacted_read_characters += sum(
                request[3] for request in segment["requests"]
            )

    read_characters = sum(row["characters"] for row in totals.values())
    ordered = dict(sorted(totals.items(), key=lambda kv: (-kv[1]["characters"], kv[0])))
    return {
        "root": root,
        "by_path": ordered,
        "files_scanned": files_scanned,
        "boundaries": boundaries,
        "paths_read": len(ordered),
        "reads_total": sum(row["reads"] for row in ordered.values()),
        "image_read_characters": acc["image_read_characters"],
        "image_repeat_characters": acc["image_repeat_characters"],
        "text_read_characters": read_characters - acc["image_read_characters"],
        "read_characters": read_characters,
        "repeat_characters": acc["repeat_characters"],
        "identical_repeat_characters": acc["identical_repeat_characters"],
        "unmapped_read_characters": acc["unmapped_read_characters"],
        "not_yet_compacted_read_characters": not_yet_compacted_read_characters,
    }


PATH_ROWS = 8


def _thousands(value):
    return "{:,}".format(int(value))


def render_rereads_markdown(payload):
    """Print what repeated reads cost, as characters — never as tokens."""
    payload = payload or {}
    read_characters = payload.get("read_characters") or 0
    by_path = payload.get("by_path") or {}
    if not read_characters:
        return (
            "no compacted `Read` result to charge under `%s` (scanned %s transcripts)\n"
            % (payload.get("root"), payload.get("files_scanned") or 0)
        )

    reads_total = sum(row.get("reads") or 0 for row in by_path.values())
    repeat_total = payload.get("repeat_characters") or 0
    lines = [
        "rereads under `%s`: %s paths were read more than once out of %s paths (%s reads)"
        % (
            payload.get("root"),
            _thousands(sum(1 for row in by_path.values() if (row.get("reads") or 0) > 1)),
            _thousands(len(by_path)),
            _thousands(reads_total),
        ),
        "",
    ]
    for name, row in list(by_path.items())[:PATH_ROWS]:
        characters = row.get("characters") or 0
        repeat = row.get("repeat_characters") or 0
        share = (100.0 * repeat / characters) if characters else 0.0
        lines.append(
            "- %-44s %3s reads %12s chars  %s repeat (%.2f%%)"
            % (name, _thousands(row.get("reads") or 0), _thousands(characters), _thousands(repeat), share)
        )
    if len(by_path) > PATH_ROWS:
        hidden = list(by_path.items())[PATH_ROWS:]
        lines.append(
            "- %s more paths" % _thousands(len(hidden))
        )
    lines.append("")
    lines.append(
        "counted: %s characters across %s reads of %s paths, %s of them repeat reads "
        "(characters, not tokens; the receipt's `preTokens` stays the only token count)"
        % (
            _thousands(read_characters),
            _thousands(reads_total),
            _thousands(len(by_path)),
            _thousands(repeat_total),
        )
    )
    identical = payload.get("identical_repeat_characters") or 0
    if identical:
        lines.append(
            "same range re-read: %s characters (%s reads) revisited an offset/limit "
            "already read — a true duplicate, not new lines"
            % (_thousands(identical), _thousands(sum(row.get("identical_repeats") or 0 for row in by_path.values())))
        )
    pending = payload.get("not_yet_compacted_read_characters") or 0
    if pending:
        lines.append(
            "not yet compacted: %s characters read after the last boundary, reported and not charged"
            % _thousands(pending)
        )
    unmapped = payload.get("unmapped_read_characters") or 0
    if unmapped:
        lines.append(
            "unattributed: %s characters of tool output name no readable call in the transcript"
            % _thousands(unmapped)
        )
    return "\n".join(lines) + "\n"
