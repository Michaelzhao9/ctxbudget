"""Discovery of runtime-composed context sources.

Read-only by construction: files are opened for reading, MCP servers are never
spawned, and any tool schema must be supplied by the caller as a JSON dump.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .parse import parse_frontmatter, parse_mcp_servers, relative_label
from .tokens import estimate_tokens

INSTRUCTION_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
}
COPILOT_SUFFIX = os.path.join(".github", "copilot-instructions.md")
MCP_CONFIG_NAMES = {".mcp.json", "mcp.json", "claude_desktop_config.json", "mcp_config.json"}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
    ".next",
    "target",
    ".idea",
}
GLOBAL_INSTRUCTION_PATHS = (
    os.path.join(".claude", "CLAUDE.md"),
    os.path.join(".codex", "AGENTS.md"),
    os.path.join(".cursor", "AGENTS.md"),
    os.path.join(".gemini", "GEMINI.md"),
)


@dataclass
class Source:
    kind: str
    label: str
    path: str
    scope: str
    text: str
    tokens: int
    per_turn: bool
    detail: str = ""


@dataclass
class McpServer:
    name: str
    command: str
    config_path: str
    tool_count: int = 0
    schema_tokens: int = 0


@dataclass
class Scan:
    root: str
    sources: List[Source] = field(default_factory=list)
    servers: List[McpServer] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def per_turn_sources(self):
        return [s for s in self.sources if s.per_turn]

    @property
    def per_turn_tokens(self):
        return sum(s.tokens for s in self.per_turn_sources)

    @property
    def installed_tokens(self):
        return sum(s.tokens for s in self.sources)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _walk(root, max_depth):
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for current, dirs, files in os.walk(root, followlinks=False):
        depth = current.rstrip(os.sep).count(os.sep) - base_depth
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")
        )
        if depth >= max_depth:
            dirs[:] = []
        yield current, files


def _instruction_source(path, root, scope):
    text = _read(path)
    if scope == "global":
        label = "~/" + os.path.relpath(path, os.path.expanduser("~")).replace(os.sep, "/")
    else:
        label = relative_label(path, root)
    return Source(
        kind="instruction",
        label=label,
        path=path,
        scope=scope,
        text=text,
        tokens=estimate_tokens(text),
        per_turn=True,
        detail="injected into the system prompt on every turn",
    )


def _skill_sources(path, root):
    text = _read(path)
    meta, body = parse_frontmatter(text)
    label = relative_label(path, root)
    description = (meta.get("description") or "").strip()
    out = []
    if description:
        out.append(
            Source(
                kind="skill-description",
                label=label + "#description",
                path=path,
                scope="project",
                text=description,
                tokens=estimate_tokens(description),
                per_turn=True,
                detail="skill name + description listed in the prompt every turn",
            )
        )
    out.append(
        Source(
            kind="skill-body",
            label=label + "#body",
            path=path,
            scope="project",
            text=body,
            tokens=estimate_tokens(body),
            per_turn=False,
            detail="loaded only when the skill is used",
        )
    )
    return out, description, label


def _tool_sources(server_name, tools, config_path):
    sources = []
    total = 0
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or tool.get("title") or "tool-%d" % index)
        payload = json.dumps(tool, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        tokens = estimate_tokens(payload)
        total += tokens
        sources.append(
            Source(
                kind="mcp-schema",
                label="mcp:%s/%s" % (server_name, name),
                path=config_path,
                scope="project",
                text=payload,
                tokens=tokens,
                per_turn=True,
                detail="tool definition sent with the request every turn",
            )
        )
    return sources, total


def load_mcp_servers(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return parse_mcp_servers(payload)


def scan(root, include_global=False, tool_dump=None, max_depth=8, extra_mcp_configs=None):
    """Walk ``root`` and return a :class:`Scan` of per-turn and on-demand context."""
    root = os.path.abspath(root)
    result = Scan(root=root)
    seen = set()
    instruction_count = 0

    def add(source):
        key = (source.kind, os.path.abspath(source.path), source.label)
        if key in seen:
            return
        seen.add(key)
        result.sources.append(source)

    for current, files in _walk(root, max_depth):
        for filename in sorted(files):
            full = os.path.join(current, filename)
            rel = relative_label(full, root)
            if filename in INSTRUCTION_NAMES or rel.replace(os.sep, "/").endswith(
                COPILOT_SUFFIX.replace(os.sep, "/")
            ):
                add(_instruction_source(full, root, "project"))
                instruction_count += 1
            elif filename == "SKILL.md":
                sources, description, label = _skill_sources(full, root)
                for source in sources:
                    add(source)
                if not description:
                    result.warnings.append(
                        "%s has no description in its frontmatter; it costs nothing "
                        "per turn but the agent cannot choose it" % label
                    )
            elif filename in MCP_CONFIG_NAMES or rel.replace(os.sep, "/").endswith(
                "/mcp.json"
            ):
                _collect_servers(result, full)

    for extra in extra_mcp_configs or []:
        _collect_servers(result, os.path.abspath(extra))

    if include_global:
        home = os.path.expanduser("~")
        for rel in GLOBAL_INSTRUCTION_PATHS:
            candidate = os.path.join(home, rel)
            if os.path.isfile(candidate):
                add(_instruction_source(candidate, root, "global"))

    if instruction_count > 1:
        result.warnings.append(
            "%d instruction files found in this tree; a session rooted in a "
            "subdirectory usually loads only the nearest one, so this total is an "
            "upper bound" % instruction_count
        )

    _apply_tool_dump(result, tool_dump)
    _warn_unpriced(result)
    result.sources.sort(key=lambda s: (not s.per_turn, -s.tokens, s.label))
    return result


def _collect_servers(result, config_path):
    servers = load_mcp_servers(config_path)
    if servers is None:
        result.warnings.append("could not parse MCP config: %s" % config_path)
        return
    known = {s.name for s in result.servers}
    for server in servers:
        if server.name in known:
            continue
        known.add(server.name)
        result.servers.append(
            McpServer(name=server.name, command=server.command, config_path=config_path)
        )


def _apply_tool_dump(result, tool_dump):
    if not tool_dump:
        return
    by_name: Dict[str, McpServer] = {s.name: s for s in result.servers}
    for server_name, tools in tool_dump.items():
        server = by_name.get(server_name)
        if server is None:
            result.warnings.append(
                "tool schemas supplied for %r, which is not declared in any MCP config; "
                "priced anyway" % server_name
            )
            server = McpServer(name=server_name, command="", config_path="(tool dump)")
            result.servers.append(server)
            by_name[server_name] = server
        sources, total = _tool_sources(server_name, tools, server.config_path)
        for source in sources:
            result.sources.append(source)
        server.tool_count = len(sources)
        server.schema_tokens = total


def _warn_unpriced(result):
    for server in result.servers:
        if server.tool_count == 0:
            result.warnings.append(
                "MCP server %r declares no tool schema in this scan; its per-turn cost "
                "is unknown. Dump its tools to JSON and pass --mcp-tools to price it."
                % server.name
            )
