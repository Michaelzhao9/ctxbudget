"""Parsing of the three context-bearing file shapes.

Everything here is stdlib-only and never executes anything it reads.
"""

import json
import os
from dataclasses import dataclass


@dataclass
class ServerInfo:
    name: str
    command: str


def _strip_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text):
    """Split ``---`` YAML-ish frontmatter from the body.

    Returns ``(meta, body)``. Only flat ``key: value`` pairs and ``>``/``|``
    block scalars are understood - enough for SKILL.md frontmatter, deliberately
    not a YAML implementation.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.lstrip("\n").startswith("---"):
        return {}, text

    lines = normalized.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if lines[start].strip() != "---":
        return {}, text

    end = None
    for idx in range(start + 1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            end = idx
            break
    if end is None:
        return {}, text

    meta = {}
    block_key = None
    block_lines = []
    block_style = None

    def flush():
        if block_key is not None:
            joiner = "\n" if block_style == "|" else " "
            meta[block_key] = joiner.join(block_lines).strip()

    for raw in lines[start + 1:end]:
        if block_key is not None:
            if raw.strip() == "" or raw.startswith((" ", "\t")):
                block_lines.append(raw.strip())
                continue
            flush()
            block_key, block_lines, block_style = None, [], None
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            block_key = key
            block_style = value[0]
            block_lines = []
            continue
        meta[key] = _strip_quotes(value)
    flush()

    body = "\n".join(lines[end + 1:])
    return meta, body


def _stringify_command(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value)
    return str(value)


def parse_mcp_servers(config):
    """Extract declared MCP servers from a config object.

    Understands the ``{"mcpServers": {...}}`` shape used by Claude Code,
    Claude Desktop and Cursor, and the ``{"mcp": {"servers": {...}}}`` shape used
    by VS Code. Unknown shapes yield an empty list rather than an exception.
    """
    if not isinstance(config, dict):
        return []
    servers = None
    if isinstance(config.get("mcpServers"), dict):
        servers = config["mcpServers"]
    else:
        nested = config.get("mcp")
        if isinstance(nested, dict) and isinstance(nested.get("servers"), dict):
            servers = nested["servers"]
    if not servers:
        return []

    out = []
    for name, spec in servers.items():
        command = ""
        if isinstance(spec, dict):
            if spec.get("url"):
                command = _stringify_command(spec.get("url"))
            else:
                parts = [spec.get("command")] + list(spec.get("args") or [])
                command = _stringify_command([p for p in parts if p is not None])
        elif isinstance(spec, str):
            command = spec
        out.append(ServerInfo(name=str(name), command=command.strip()))
    return out


def _as_tool_list(value):
    if isinstance(value, list):
        return [t for t in value if isinstance(t, dict)]
    if isinstance(value, dict) and isinstance(value.get("tools"), list):
        return [t for t in value["tools"] if isinstance(t, dict)]
    return None


def parse_tool_dump(payload):
    """Normalize a dumped MCP tool-schema payload into ``{server: [tools]}``.

    Accepted shapes: a list of tool objects, ``{"tools": [...]}``, or a map of
    server name to either form. Raises ``ValueError`` on anything else.
    """
    if isinstance(payload, list):
        tools = _as_tool_list(payload)
        if tools is None:
            raise ValueError("tool dump must be a list of tool objects")
        return {"(unnamed)": tools}

    if isinstance(payload, dict):
        direct = _as_tool_list(payload)
        if direct is not None:
            return {"(unnamed)": direct}
        result = {}
        for key, value in payload.items():
            tools = _as_tool_list(value)
            if tools is not None:
                result[str(key)] = tools
        if result:
            return result
        raise ValueError(
            "tool dump object must contain tools, or map server names to tool lists"
        )

    raise ValueError("tool dump must be a JSON object or array, got %s" % type(payload).__name__)


def read_json(path):
    """Read JSON from ``path`` with an error message that names the file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise ValueError("cannot read %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise ValueError("%s is not valid JSON: %s" % (path, exc))


def relative_label(path, root):
    """Path shown in reports: relative to root when possible, else basename-ish."""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:  # different drives on Windows
        return path
    if rel.startswith(".."):
        return path
    return rel
