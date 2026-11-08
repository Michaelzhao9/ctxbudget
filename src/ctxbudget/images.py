"""Are the pictures in a transcript new, or the same screenshot pasted again.

``--rereads`` showed that 97% of the characters charged to compacted ``Read`` results
are base64 image payloads. Base64 length says nothing about what the picture costs, and
it cannot tell "one screenshot pasted twenty times" from "twenty different screenshots".
Those two call for opposite advice — stop re-pasting vs. use pictures less — so this
mode measures the pictures themselves: content digest for identity, and pixel area read
from the image header for size.

Pixels are reported and never converted to tokens: the provider bills images by its own
rule, and this tool refuses to invent a rate.
"""

import base64
import hashlib
import json
import os
import re
import struct

# Claude Code writes a conversation to <session-id>.jsonl and its subagent
# transcripts to <session-id>/subagents/agent-*.jsonl, so the same conversation
# can arrive as several files.
SESSION_ID = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def session_of(relpath):
    """The session id the path carries, or None for a file that names no session."""
    for part in str(relpath).split(os.sep):
        match = SESSION_ID.match(part)
        if match:
            return match.group(1)
    return None

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SOF_MARKERS = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
# Only the first bytes of a picture are needed for its header; 128 KiB of base64 is
# enough for a PNG IHDR and for a JPEG whose SOF follows ordinary metadata segments.
HEADER_BASE64_CHARACTERS = 131072


def _png_size(raw):
    if len(raw) < 24 or not raw.startswith(PNG_SIGNATURE):
        return None
    width, height = struct.unpack(">II", raw[16:24])
    return (width, height)


def _jpeg_size(raw):
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None
    position = 2
    limit = len(raw)
    while position + 9 < limit:
        if raw[position] != 0xFF:
            position += 1
            continue
        marker = raw[position + 1]
        if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        length = struct.unpack(">H", raw[position + 2 : position + 4])[0]
        if marker in SOF_MARKERS:
            height, width = struct.unpack(">HH", raw[position + 5 : position + 9])
            return (width, height)
        position += 2 + length
    return None


def _gif_size(raw):
    if len(raw) < 10 or raw[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", raw[6:10])
    return (width, height)


def image_size(raw):
    """Width and height from the file header, or ``None`` when this tool cannot read it."""
    for reader in (_png_size, _jpeg_size, _gif_size):
        size = reader(raw)
        if size:
            return size
    return None


def _size_from_base64(data):
    try:
        raw = base64.b64decode(data[:HEADER_BASE64_CHARACTERS], validate=False)
    except (ValueError, TypeError):
        return None
    return image_size(raw)


def _content_blocks(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _images_in(block):
    """The image parts of a tool result, in order."""
    content = block.get("content")
    if not isinstance(content, list):
        return []
    return [
        part
        for part in content
        if isinstance(part, dict) and part.get("type") == "image" and isinstance(part.get("source"), dict)
    ]


def _direct_images(record):
    """Image blocks pasted straight into a message, not returned by a tool."""
    return [
        block
        for block in _content_blocks(record)
        if block.get("type") == "image" and isinstance(block.get("source"), dict)
    ]


def _empty_payload(root):
    return {
        "root": root,
        "files_scanned": 0,
        "transcripts_with_images": 0,
        "placements": 0,
        "distinct_images": 0,
        "repeat_placements": 0,
        "repeat_pixels": 0,
        "same_transcript_repeat_placements": 0,
        "cross_transcript_extra_placements": 0,
        "shared_images": 0,
        "characters": 0,
        "pixels": 0,
        "unknown_dimension_placements": 0,
        "compacted_placements": 0,
        "compacted_characters": 0,
        "compacted_pixels": 0,
        "compacted_distinct_images": 0,
        "compacted_repeat_placements": 0,
        "compacted_repeat_pixels": 0,
        "prompt_placements": 0,
        "prompt_pixels": 0,
        "prompt_repeat_placements": 0,
        "skipped_copy_placements": 0,
        "skipped_copy_characters": 0,
        "media_types": {},
        "by_image": {},
        "by_transcript": [],
        "by_session": [],
        "sessions_with_images": 0,
    }


def scan_images(root):
    """Attribute every image that reached a transcript to its content digest.

    The repeat counters are deliberately order-free: "the second time" depends on which
    transcript a directory walk happens to read first, so per-transcript repeats and
    extra copies in other transcripts are counted separately and a total repeat count is
    reported as ``placements - distinct_images``, which no ordering can change.
    """
    root = os.path.abspath(os.path.expanduser(str(root)))
    payload = _empty_payload(root)
    sizes = {}
    rows = payload["by_image"]
    media_types = payload["media_types"]
    seen_digests = set()
    compacted_digests = set()
    compacted_pixels = {}
    same_transcript_repeats = 0
    session_rows = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(current, name)
            if os.path.islink(full):
                continue
            payload["files_scanned"] += 1
            in_this_transcript = {}
            transcript_digest_pixels = {}
            transcript_pixels = 0
            transcript_characters = 0
            transcript_placements = 0
            has_images = False
            pending_compacted = []

            def flush(pending):
                for digest, characters, pixels in pending:
                    compacted_digests.add(digest)
                    compacted_pixels[digest] = pixels
                    payload["compacted_placements"] += 1
                    payload["compacted_characters"] += characters
                    payload["compacted_pixels"] += pixels
                pending.clear()

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
                            flush(pending_compacted)
                            continue
                        record_type = record.get("type")
                        if record_type not in ("user", "assistant", "api-request-blob"):
                            continue
                        if record_type == "api-request-blob":
                            # A copy of the request body, not a second look at the picture.
                            # Counting it would double every cost it holds.
                            copies = []
                            for block in _content_blocks(record):
                                copies.extend(_images_in(block))
                            # Direct prompt images are collected once, not once per block.
                            copies.extend(_direct_images(record))
                            payload["skipped_copy_placements"] += len(copies)
                            payload["skipped_copy_characters"] += sum(
                                len((part.get("source") or {}).get("data") or "")
                                for part in copies
                            )
                            continue
                        for block in _content_blocks(record):
                            if block.get("type") == "tool_result":
                                origin = "tool"
                                parts = _images_in(block)
                            elif block.get("type") == "image" and isinstance(block.get("source"), dict):
                                origin = "prompt"
                                parts = [block]
                            else:
                                continue
                            for part in parts:
                                source = part["source"]
                                data = source.get("data")
                                if not isinstance(data, str) or not data:
                                    continue
                                media_type = source.get("media_type") or "unknown"
                                digest = hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
                                characters = len(data)
                                if digest not in sizes:
                                    sizes[digest] = _size_from_base64(data)
                                size = sizes[digest]
                                pixels = (size[0] * size[1]) if size else 0
                                has_images = True
                                payload["placements"] += 1
                                payload["characters"] += characters
                                payload["pixels"] += pixels
                                transcript_placements += 1
                                transcript_characters += characters
                                transcript_pixels += pixels
                                transcript_digest_pixels[digest] = pixels
                                bucket = media_types.setdefault(
                                    media_type, {"placements": 0, "characters": 0, "pixels": 0}
                                )
                                bucket["placements"] += 1
                                bucket["characters"] += characters
                                bucket["pixels"] += pixels
                                if origin == "prompt":
                                    payload["prompt_placements"] += 1
                                    payload["prompt_pixels"] += pixels
                                    # A prompt paste is a repeat only against the context of
                                    # its own transcript; another file holding the same
                                    # picture says nothing about this conversation.
                                    if digest in in_this_transcript:
                                        payload["prompt_repeat_placements"] += 1
                                pending_compacted.append((digest, characters, pixels))
                                if digest not in rows:
                                    rows[digest] = {
                                        "digest": digest,
                                        "media_type": media_type,
                                        "width": size[0] if size else None,
                                        "height": size[1] if size else None,
                                        "pixels": pixels,
                                        "placements": 0,
                                        "characters": 0,
                                        "transcripts": [],
                                    }
                                row = rows[digest]
                                row["placements"] += 1
                                row["characters"] += characters
                                if name not in row["transcripts"]:
                                    row["transcripts"].append(name)
                                in_this_transcript[digest] = in_this_transcript.get(digest, 0) + 1
                                seen_digests.add(digest)
            except OSError:
                continue
            same_transcript_repeats += sum(count - 1 for count in in_this_transcript.values())
            if has_images:
                payload["transcripts_with_images"] += 1
                rel = os.path.relpath(full, root)
                payload["by_transcript"].append(
                    {
                        "transcript": rel,
                        "placements": transcript_placements,
                        "distinct_images": len(transcript_digest_pixels),
                        "characters": transcript_characters,
                        "pixels": transcript_pixels,
                        "repeat_pixels": transcript_pixels - sum(transcript_digest_pixels.values()),
                    }
                )
                # The same placements folded by conversation. The id is the
                # conversation's identity, so its subagent files join it even
                # though they live in a subdirectory; a file that names no id
                # stands alone rather than being pooled with its neighbours.
                session = session_of(rel)
                bucket = session_rows.setdefault(
                    session or rel,
                    {
                        "session": session or rel,
                        "project": (os.path.dirname(rel) or ".").split(os.sep)[0],
                        "dirs": set(),
                        "projects": set(),
                        "files": 0,
                        "subagent_files": 0,
                        "subagent_pixels": 0,
                        "placements": 0,
                        "characters": 0,
                        "pixels": 0,
                        "digests": {},
                    },
                )
                bucket["dirs"].add(os.path.dirname(rel))
                # A project root is the first directory under the transcripts
                # root: a resume into another checkout moves the root, while
                # <session>/subagents stays inside the same one.
                bucket["projects"].add(
                    (os.path.dirname(rel) or ".").split(os.sep)[0]
                )
                bucket["files"] += 1
                if "subagents" in rel.split(os.sep):
                    bucket["subagent_files"] += 1
                    bucket["subagent_pixels"] += transcript_pixels
                bucket["placements"] += transcript_placements
                bucket["characters"] += transcript_characters
                bucket["pixels"] += transcript_pixels
                for digest, pixels in transcript_digest_pixels.items():
                    bucket["digests"][digest] = pixels
    payload["by_transcript"] = sorted(
        payload["by_transcript"], key=lambda row: (-row["pixels"], row["transcript"])
    )
    payload["sessions_with_images"] = len(session_rows)
    payload["by_session"] = [
        {
            "session": bucket["session"],
            "project": bucket["project"],
            "dirs": len(bucket["dirs"]),
            "projects": len(bucket["projects"]),
            "files": bucket["files"],
            "subagent_files": bucket["subagent_files"],
            "subagent_pixels": bucket["subagent_pixels"],
            "placements": bucket["placements"],
            "distinct_images": len(bucket["digests"]),
            "characters": bucket["characters"],
            "pixels": bucket["pixels"],
            "repeat_pixels": bucket["pixels"] - sum(bucket["digests"].values()),
        }
        for bucket in sorted(
            session_rows.values(), key=lambda row: (-row["pixels"], row["session"])
        )
    ]
    payload["distinct_images"] = len(rows)
    payload["repeat_placements"] = payload["placements"] - payload["distinct_images"]
    payload["repeat_pixels"] = payload["pixels"] - sum(row["pixels"] for row in rows.values())
    payload["same_transcript_repeat_placements"] = same_transcript_repeats
    payload["cross_transcript_extra_placements"] = (
        payload["repeat_placements"] - same_transcript_repeats
    )
    payload["shared_images"] = sum(1 for row in rows.values() if len(row["transcripts"]) > 1)
    payload["compacted_distinct_images"] = len(compacted_digests)
    payload["compacted_repeat_placements"] = (
        payload["compacted_placements"] - len(compacted_digests)
    )
    payload["compacted_repeat_pixels"] = payload["compacted_pixels"] - sum(
        compacted_pixels.values()
    )
    payload["unknown_dimension_placements"] = sum(
        row["placements"] for row in rows.values() if row["width"] is None
    )
    payload["by_image"] = dict(sorted(rows.items(), key=lambda kv: (-kv[1]["pixels"], kv[0])))
    return payload


# How many rows each ranking prints unless the caller asks for another number.
DEFAULT_ROWS = 8


def _thousands(value):
    return "{:,}".format(int(value))


def _short(name, width):
    """Keep a column aligned without hiding which transcript it is past `width`."""
    name = str(name)
    if len(name) <= width:
        return name
    return "..." + name[-(width - 3) :]


def render_images_markdown(payload, rows=None):
    """Say whether the pictures are new ones or the same one placed again."""
    payload = payload or {}
    placements = payload.get("placements") or 0
    if not placements:
        return (
            "no image in any transcript under `%s` (scanned %s files)\n"
            % (payload.get("root"), _thousands(payload.get("files_scanned") or 0))
        )
    distinct = payload.get("distinct_images") or 0
    repeats = payload.get("repeat_placements") or 0
    pixels = payload.get("pixels") or 0
    repeat_pixels = payload.get("repeat_pixels") or 0
    lines = [
        "images under `%s`: %s placements of %s distinct pictures in %s transcripts"
        % (
            payload.get("root"),
            _thousands(placements),
            _thousands(distinct),
            _thousands(payload.get("transcripts_with_images") or 0),
        ),
        "- the same picture placed again: %s placements, %s of %s pixels (%.2f%%)"
        % (
            _thousands(repeats),
            _thousands(repeat_pixels),
            _thousands(pixels),
            (100.0 * repeat_pixels / pixels) if pixels else 0.0,
        ),
        "  - twice inside one transcript: %s (order-free: per-transcript counts minus one)"
        % _thousands(payload.get("same_transcript_repeat_placements") or 0),
        "  - an extra copy in another transcript: %s; %s pictures appear in more than one"
        " transcript (a resumed session writes a second file, so this is not proof of a"
        " re-paste)"
        % (
            _thousands(payload.get("cross_transcript_extra_placements") or 0),
            _thousands(payload.get("shared_images") or 0),
        ),
        "- pasted into a prompt (not returned by a tool): %s placements, %s pixels, %s of"
        " them a picture already in that transcript's context"
        % (
            _thousands(payload.get("prompt_placements") or 0),
            _thousands(payload.get("prompt_pixels") or 0),
            _thousands(payload.get("prompt_repeat_placements") or 0),
        ),
        "- charged to a compaction: %s placements of %s distinct pictures (%s placements,"
        " %s pixels beyond the first copy), %s characters"
        % (
            _thousands(payload.get("compacted_placements") or 0),
            _thousands(payload.get("compacted_distinct_images") or 0),
            _thousands(payload.get("compacted_repeat_placements") or 0),
            _thousands(payload.get("compacted_repeat_pixels") or 0),
            _thousands(payload.get("compacted_characters") or 0),
        ),
        "  (that is the subset `--rereads` charges, except that `--rereads` counts Read results only while this counts every tool)",
        "",
    ]
    limit = DEFAULT_ROWS if rows is None else int(rows)
    by_image = payload.get("by_image") or {}
    for digest, row in list(by_image.items())[:limit]:
        size = (
            "%dx%d" % (row["width"], row["height"])
            if row.get("width") is not None
            else "unknown size"
        )
        lines.append(
            "- %-16s %-12s %10s  %3s placements  %12s pixels"
            % (
                digest,
                size,
                row.get("media_type") or "unknown",
                _thousands(row.get("placements") or 0),
                _thousands(row.get("pixels") or 0),
            )
        )
    if len(by_image) > limit:
        lines.append("- %s more pictures" % _thousands(len(by_image) - limit))
    by_transcript = payload.get("by_transcript") or []
    if by_transcript:
        lines.append("")
        lines.append(
            "transcripts that held the most pixels (pixels, not tokens; share of every"
            " pixel in this scan):"
        )
        for row in by_transcript[:limit]:
            lines.append(
                "- %-56s %4s placements  %3s pictures  %12s px  %5.1f%%"
                % (
                    _short(row.get("transcript") or "?", 56),
                    _thousands(row.get("placements") or 0),
                    _thousands(row.get("distinct_images") or 0),
                    _thousands(row.get("pixels") or 0),
                    (100.0 * (row.get("pixels") or 0) / pixels) if pixels else 0.0,
                )
            )
        if len(by_transcript) > limit:
            lines.append(
                "- %s more transcripts with pictures"
                % _thousands(len(by_transcript) - limit)
            )
    by_session = payload.get("by_session") or []
    if by_session and len(by_session) < len(by_transcript):
        # Only worth printing when a conversation actually arrived as several
        # files; otherwise the block repeats the file ranking row for row.
        lines.append("")
        lines.append(
            "the same pixels grouped by session id (a resumed conversation writes more"
            " than one file, so the per-file ranking splits it):"
        )
        for row in by_session[:limit]:
            label = str(row.get("session") or "?")
            # A session id is long; a file that names none is shown by its name.
            label = label[:8] if SESSION_ID.match(label) else _short(label, 14)
            # A session id is the conversation, not the checkout: when its files
            # sit in more than one project directory the row has to say so, or a
            # reader counting directories adds the conversation up twice.
            dirs = row.get("dirs") or 0
            projects = row.get("projects") or 0
            mark = ("  %d dirs" % dirs) if dirs > 1 else ""
            if projects > 1:
                mark += " (%d project(s))" % projects
            lines.append(
                "- %-12s %2s file(s)  %4s placements  %3s pictures  %12s px  %5.1f%%%s"
                % (
                    label,
                    _thousands(row.get("files") or 0),
                    _thousands(row.get("placements") or 0),
                    _thousands(row.get("distinct_images") or 0),
                    _thousands(row.get("pixels") or 0),
                    (100.0 * (row.get("pixels") or 0) / pixels) if pixels else 0.0,
                    mark,
                )
            )
        if len(by_session) > limit:
            lines.append(
                "- %s more session(s) with pictures"
                % _thousands(len(by_session) - limit)
            )
        multi = [row for row in by_session if (row.get("files") or 0) > 1]
        if multi:
            multi_pixels = sum(row.get("pixels") or 0 for row in multi)
            lines.append(
                "- %s session(s) hold more than one file and %s of the pixels"
                % (
                    _thousands(len(multi)),
                    ("%.1f%%" % (100.0 * multi_pixels / pixels)) if pixels else "0.0%",
                )
            )
            subagent_pixels = sum(row.get("subagent_pixels") or 0 for row in multi)
            if subagent_pixels:
                lines.append(
                    "- %s of those pixels sit in subagent transcripts (%s file(s))"
                    % (
                        "%.1f%%" % (100.0 * subagent_pixels / multi_pixels),
                        _thousands(
                            sum(row.get("subagent_files") or 0 for row in multi)
                        ),
                    )
                )
                lines.append(
                    "  a subagent runs in its own context, so a session row is what one"
                    " conversation cost, not what one window held"
                )
        # Two different facts share the word "directory": a conversation resumed
        # from a second checkout (another project root) and a conversation whose
        # subagent wrote into <session>/subagents. Only the first one is a merge
        # the per-directory view would count twice, so they are counted apart.
        moved = [row for row in by_session if (row.get("projects") or 0) > 1]
        if moved:
            moved_pixels = sum(row.get("pixels") or 0 for row in moved)
            lines.append(
                "- %s session(s) hold files in more than one project directory and %s"
                " of the pixels"
                % (
                    _thousands(len(moved)),
                    ("%.1f%%" % (100.0 * moved_pixels / pixels)) if pixels else "0.0%",
                )
            )
            lines.append(
                "  the checkout changed, not the conversation, so a per-directory"
                " ranking would count that session twice"
            )
        nested = [
            row
            for row in by_session
            if (row.get("dirs") or 0) > (row.get("projects") or 0)
        ]
        if nested:
            lines.append(
                "- %s session(s) also keep a file in a subdirectory of their own project"
                " (a subagent transcript), which is not another checkout"
                % _thousands(len(nested))
            )
        if multi:
            # The two sentences above count overlapping facts, so adding them does
            # not give the multi-file total. Split the total into classes that are
            # disjoint by construction and print the arithmetic the reader would
            # otherwise have to guess at.
            crossed = [row for row in multi if (row.get("projects") or 0) > 1]
            nested_only = [
                row
                for row in multi
                if (row.get("projects") or 0) <= 1
                and (row.get("dirs") or 0) > (row.get("projects") or 0)
            ]
            side_by_side = [
                row
                for row in multi
                if (row.get("projects") or 0) <= 1
                and (row.get("dirs") or 0) <= (row.get("projects") or 0)
            ]
            parts = []
            if nested_only:
                parts.append(
                    "%s kept a file in a subdirectory" % _thousands(len(nested_only))
                )
            if crossed:
                parts.append("%s changed checkout" % _thousands(len(crossed)))
            if side_by_side:
                parts.append(
                    "%s kept several files in one directory"
                    % _thousands(len(side_by_side))
                )
            lines.append(
                "  of the %s multi-file session(s): %s"
                % (_thousands(len(multi)), ", ".join(parts))
            )
    lines.append("")
    lines.append(
        "counted: %s base64 characters and %s pixels; characters are a bad proxy for the"
        " price of a picture, so pixels come from the image header and are never converted"
        " to a model's token count here"
        % (_thousands(payload.get("characters") or 0), _thousands(pixels))
    )
    skipped = payload.get("skipped_copy_placements") or 0
    if skipped:
        lines.append(
            "skipped: %s placements (%s characters) sit in `api-request-blob` copies of a"
            " request body, so they are copies of a picture already counted, not more"
            " pictures"
            % (_thousands(skipped), _thousands(payload.get("skipped_copy_characters") or 0))
        )
    unknown = payload.get("unknown_dimension_placements") or 0
    if unknown:
        lines.append(
            "unknown size: %s placements whose format this tool cannot read (webp, or a"
            " header beyond the first %s base64 characters); they are counted as"
            " placements and contribute no pixels"
            % (_thousands(unknown), _thousands(HEADER_BASE64_CHARACTERS))
        )
    return "\n".join(lines) + "\n"
