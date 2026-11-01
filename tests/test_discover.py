import os

from ctxbudget.discover import scan

SKILL_WITH_DESC = """---
name: pdf-tools
description: Read, fill, and split PDF files when the user mentions PDFs.
license: MIT
---

# PDF tools

Long body that is only loaded when the skill is actually used.
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_instruction_file_is_per_turn_context(tmp_path):
    write(tmp_path / "AGENTS.md", "# Rules\nAlways run pytest before committing.\n")
    result = scan(str(tmp_path), include_global=False)
    kinds = [s.kind for s in result.sources]
    assert "instruction" in kinds
    instruction = [s for s in result.sources if s.kind == "instruction"][0]
    assert instruction.per_turn is True
    assert instruction.tokens > 0
    assert instruction.label == "AGENTS.md"
    assert result.per_turn_tokens == instruction.tokens


def test_skill_description_is_per_turn_but_body_is_on_demand(tmp_path):
    write(tmp_path / "skills" / "pdf-tools" / "SKILL.md", SKILL_WITH_DESC)
    result = scan(str(tmp_path), include_global=False)
    desc = [s for s in result.sources if s.kind == "skill-description"]
    body = [s for s in result.sources if s.kind == "skill-body"]
    assert len(desc) == 1 and len(body) == 1
    assert desc[0].per_turn is True
    assert body[0].per_turn is False
    assert desc[0].tokens < body[0].tokens
    assert "PDF" in desc[0].text
    assert "only loaded" not in desc[0].text
    assert result.per_turn_tokens == desc[0].tokens
    assert result.installed_tokens == desc[0].tokens + body[0].tokens


def test_skill_without_description_warns_and_costs_only_body(tmp_path):
    write(tmp_path / "skills" / "bare" / "SKILL.md", "---\nname: bare\n---\nbody text\n")
    result = scan(str(tmp_path), include_global=False)
    assert any("description" in w for w in result.warnings)
    assert result.per_turn_tokens == 0
    assert result.installed_tokens > 0


def test_mcp_tool_schema_is_priced_per_server(tmp_path):
    write(
        tmp_path / ".mcp.json",
        '{"mcpServers": {"fs": {"command": "npx", "args": ["-y", "fs-server"]}}}',
    )
    dump = {
        "fs": [
            {
                "name": "read_file",
                "description": "Read a file from disk",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]
    }
    result = scan(str(tmp_path), include_global=False, tool_dump=dump)
    servers = {s.name: s for s in result.servers}
    assert servers["fs"].command == "npx -y fs-server"
    assert servers["fs"].tool_count == 1
    assert servers["fs"].schema_tokens > 0
    schema_sources = [s for s in result.sources if s.kind == "mcp-schema"]
    assert len(schema_sources) == 1
    assert schema_sources[0].label == "mcp:fs/read_file"
    assert schema_sources[0].per_turn is True
    assert result.per_turn_tokens == schema_sources[0].tokens


def test_tool_dump_for_unknown_server_is_kept_with_a_warning(tmp_path):
    write(
        tmp_path / ".mcp.json",
        '{"mcpServers": {"fs": {"command": "npx", "args": ["fs"]}}}',
    )
    dump = {"ghost": [{"name": "boo", "inputSchema": {"type": "object"}}]}
    result = scan(str(tmp_path), include_global=False, tool_dump=dump)
    assert any("ghost" in w for w in result.warnings)
    assert [s.name for s in result.servers] == ["fs", "ghost"]


def test_declared_server_without_schema_is_reported_as_unpriced(tmp_path):
    write(tmp_path / ".mcp.json", '{"mcpServers": {"fs": {"command": "npx", "args": ["fs"]}}}')
    result = scan(str(tmp_path), include_global=False)
    assert result.servers[0].tool_count == 0
    assert any("fs" in w and "schema" in w for w in result.warnings)


def test_vendor_and_vcs_directories_are_skipped(tmp_path):
    write(tmp_path / "node_modules" / "pkg" / "SKILL.md", SKILL_WITH_DESC)
    write(tmp_path / ".git" / "SKILL.md", SKILL_WITH_DESC)
    write(tmp_path / ".venv" / "SKILL.md", SKILL_WITH_DESC)
    result = scan(str(tmp_path), include_global=False)
    assert [s for s in result.sources if s.kind == "skill-body"] == []


def test_each_file_is_counted_once(tmp_path):
    write(tmp_path / "AGENTS.md", "keep tests green\n")
    result = scan(str(tmp_path), include_global=False)
    labels = [s.label for s in result.sources]
    assert labels.count("AGENTS.md") == 1


def test_global_paths_are_opt_in(tmp_path, monkeypatch):
    home = tmp_path / "home"
    write(home / ".claude" / "CLAUDE.md", "global rules\n")
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    off = scan(str(project), include_global=False)
    on = scan(str(project), include_global=True)
    assert off.per_turn_tokens == 0
    assert on.per_turn_tokens > 0
    assert any(s.scope == "global" for s in on.sources)
