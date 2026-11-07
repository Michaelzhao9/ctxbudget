"""Attribute the content a compaction discarded to the component that carried it."""

import json
import os

COMPONENTS = (
    "prompt",
    "reminder",
    "assistant_text",
    "thinking",
    "tool_use",
    "tool_result",
    "attachment",
    "image",
    "other",
)

# Only these record types own the content they carry. A transcript can also hold
# an ``api-request-blob`` that echoes a request whose blocks are already records;
# counting it would double every character it repeats.
CONTENT_RECORD_TYPES = ("user", "assistant")
ATTACHMENT_RECORD_TYPE = "attachment"


def _text_length(value):
    """Characters of a text payload; a non-string payload has no length to claim."""
    return len(value) if isinstance(value, str) else 0


def _block_characters(block):
    """Characters the block carries, or 0 when it carries no text (an image)."""
    kind = block.get("type")
    if kind == "text":
        return _text_length(block.get("text"))
    if kind == "thinking":
        return _text_length(block.get("thinking"))
    if kind == "tool_use":
        return len(json.dumps(block.get("input"), ensure_ascii=False, sort_keys=True))
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return len(content)
        return len(json.dumps(content, ensure_ascii=False, sort_keys=True))
    return 0


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


def _component_of(block, role):
    kind = block.get("type")
    if kind == "text":
        if role != "user":
            return "assistant_text"
        text = block.get("text")
        if isinstance(text, str) and text.lstrip().startswith("<system-reminder>"):
            return "reminder"
        return "prompt"
    if kind == "thinking":
        return "thinking"
    if kind == "tool_use":
        return "tool_use"
    if kind == "tool_result":
        return "tool_result"
    if kind in ("image", "document"):
        return "image"
    return "other"


def _empty_segment():
    return {
        "components": {name: 0 for name in COMPONENTS},
        "by_tool": {},
        "unmapped_tool_characters": 0,
        "characters": 0,
    }


def _merge(into, segment):
    for name, value in segment["components"].items():
        into["components"][name] += value
    for name, row in segment["by_tool"].items():
        target = into["by_tool"].setdefault(name, {"characters": 0})
        target["characters"] += row["characters"]
    into["unmapped_tool_characters"] += segment["unmapped_tool_characters"]
    into["characters"] += segment["characters"]


def scan_components(root):
    """Walk *root* and attribute each compacted segment to the components in it.

    A segment ends where a compaction boundary sits: what follows the last
    boundary is context the harness has *not* thrown away yet, so it is counted
    separately instead of being folded into the answer.
    """
    root = os.path.abspath(os.path.expanduser(str(root)))
    totals = _empty_segment()
    boundaries = 0
    unparsed_lines = 0
    ignored_records = 0
    files_scanned = 0
    files_with_boundaries = 0
    not_yet_compacted_characters = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(current, name)
            if os.path.islink(full):
                continue
            files_scanned += 1
            tool_names = {}
            segment = _empty_segment()
            found_here = 0
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except ValueError:
                            unparsed_lines += 1
                            continue
                        if not isinstance(record, dict):
                            unparsed_lines += 1
                            continue
                        meta = record.get("compactMetadata")
                        if isinstance(meta, dict):
                            _merge(totals, segment)
                            segment = _empty_segment()
                            found_here += 1
                            boundaries += 1
                            continue
                        role = record.get("type")
                        if role not in CONTENT_RECORD_TYPES:
                            if role == ATTACHMENT_RECORD_TYPE:
                                attachment = record.get(ATTACHMENT_RECORD_TYPE)
                                characters = len(
                                    json.dumps(attachment, ensure_ascii=False, sort_keys=True)
                                )
                                segment["components"]["attachment"] += characters
                                segment["characters"] += characters
                            else:
                                ignored_records += 1
                            continue
                        for block in _content_blocks(record):
                            component = _component_of(block, role)
                            characters = _block_characters(block)
                            segment["components"][component] += characters
                            segment["characters"] += characters
                            if component == "tool_use":
                                tool_names[block.get("id")] = block.get("name")
                            elif component == "tool_result":
                                tool_name = tool_names.get(block.get("tool_use_id"))
                                if isinstance(tool_name, str) and tool_name:
                                    row = segment["by_tool"].setdefault(
                                        tool_name, {"characters": 0}
                                    )
                                    row["characters"] += characters
                                else:
                                    segment["unmapped_tool_characters"] += characters
            except OSError:
                continue
            if found_here:
                files_with_boundaries += 1
            # Whatever is still pending at EOF sits after the last boundary: the
            # harness has not thrown it away, so it is reported, not attributed.
            not_yet_compacted_characters += segment["characters"]

    tool_total = sum(row["characters"] for row in totals["by_tool"].values())
    if tool_total:
        for row in totals["by_tool"].values():
            row["share_of_tool_results"] = row["characters"] / totals["components"]["tool_result"]
    ordered_tools = dict(
        sorted(totals["by_tool"].items(), key=lambda kv: (-kv[1]["characters"], kv[0]))
    )
    return {
        "root": root,
        "components": totals["components"],
        "by_tool": ordered_tools,
        "unmapped_tool_characters": totals["unmapped_tool_characters"],
        "boundaries": boundaries,
        "files_scanned": files_scanned,
        "files_with_boundaries": files_with_boundaries,
        "counted_characters": sum(totals["components"].values()),
        "not_yet_compacted_characters": not_yet_compacted_characters,
        "unparsed_lines": unparsed_lines,
        "ignored_records": ignored_records,
    }


LABELS = {
    "tool_result": "tool results",
    "thinking": "thinking",
    "prompt": "your prompts",
    "reminder": "system reminders",
    "assistant_text": "assistant text",
    "tool_use": "tool arguments",
    "attachment": "attachments",
    "image": "images",
    "other": "other blocks",
}


TOOL_ROWS = 10


def _thousands(value):
    return "{:,}".format(int(value))


def render_components_markdown(payload):
    """Print the attribution as shares of counted characters, never as tokens."""
    payload = payload or {}
    counted = payload.get("counted_characters") or 0
    boundaries = payload.get("boundaries") or 0
    if not boundaries or not counted:
        return (
            "no compacted context to attribute under `%s`: no compaction boundary carries "
            "readable content (scanned %s transcripts)\n"
            % (payload.get("root"), payload.get("files_scanned") or 0)
        )

    lines = [
        "components behind %s compactions under `%s`"
        % (_thousands(boundaries), payload.get("root")),
        "",
    ]
    rows = [
        (name, value)
        for name, value in (payload.get("components") or {}).items()
        if value
    ]
    rows.sort(key=lambda item: (-item[1], item[0]))
    for name, value in rows:
        lines.append(
            "- %-18s %12s  %.2f%%" % (LABELS.get(name, name), _thousands(value), 100.0 * value / counted)
        )
    lines.append("")
    lines.append(
        "counted: %s characters from %s boundaries in %s transcripts "
        "(characters, not tokens; the receipt's `preTokens` stays the only token count)"
        % (
            _thousands(counted),
            _thousands(boundaries),
            _thousands(payload.get("files_with_boundaries") or 0),
        )
    )
    pending = payload.get("not_yet_compacted_characters") or 0
    if pending:
        lines.append(
            "not yet compacted: %s characters after the last boundary, reported and not attributed"
            % _thousands(pending)
        )
    by_tool = payload.get("by_tool") or {}
    if by_tool:
        lines.append("")
        lines.append("tool results by tool:")
        for name, row in list(by_tool.items())[:TOOL_ROWS]:
            lines.append(
                "- %-18s %12s  %.2f%% of tool results"
                % (name, _thousands(row.get("characters") or 0), 100.0 * (row.get("share_of_tool_results") or 0))
            )
    if len(by_tool) > TOOL_ROWS:
        hidden = list(by_tool.items())[TOOL_ROWS:]
        hidden_characters = sum(row.get("characters") or 0 for _name, row in hidden)
        lines.append(
            "- %s more tools              %12s  %.2f%% of tool results"
            % (
                len(hidden),
                _thousands(hidden_characters),
                100.0 * hidden_characters / (payload["components"].get("tool_result") or 1),
            )
        )
    unmapped = payload.get("unmapped_tool_characters") or 0
    if unmapped:
        lines.append("")
        lines.append(
            "unattributed tool output: %s characters carry a `tool_use_id` that no tool call in "
            "the transcript names" % _thousands(unmapped)
        )
    unparsed = payload.get("unparsed_lines") or 0
    if unparsed:
        lines.append("unreadable lines: %s were skipped and are not in the denominator" % _thousands(unparsed))
    return "\n".join(lines) + "\n"
