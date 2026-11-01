import json

from ctxbudget.parse import (
    parse_frontmatter,
    parse_mcp_servers,
    parse_tool_dump,
)


def test_frontmatter_is_split_from_body():
    text = (
        "---\n"
        "name: pdf\n"
        "description: Read and fill PDF forms.\n"
        "---\n"
        "\n"
        "# PDF\n"
        "Long instructions that are only loaded on demand.\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "pdf"
    assert meta["description"] == "Read and fill PDF forms."
    assert body.lstrip().startswith("# PDF")
    assert "description:" not in body


def test_file_without_frontmatter_is_all_body():
    text = "# AGENTS.md\n\nRun pytest before committing.\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_folded_description_block_is_joined():
    text = (
        "---\n"
        "name: long-skill\n"
        "description: >\n"
        "  Use when the user asks for a spreadsheet.\n"
        "  Handles xlsx and csv input.\n"
        "license: MIT\n"
        "---\n"
        "body\n"
    )
    meta, _ = parse_frontmatter(text)
    assert meta["description"] == (
        "Use when the user asks for a spreadsheet. Handles xlsx and csv input."
    )
    assert meta["license"] == "MIT"


def test_quoted_description_keeps_inner_colons():
    meta, _ = parse_frontmatter('---\ndescription: "Audit: per-turn, per-source"\n---\nx\n')
    assert meta["description"] == "Audit: per-turn, per-source"


def test_crlf_frontmatter_is_parsed():
    text = "---\r\nname: crlf\r\ndescription: windows file\r\n---\r\nbody\r\n"
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "crlf"
    assert "windows file" in meta["description"]
    assert "body" in body


def test_mcp_servers_from_claude_style_config():
    cfg = {
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@mcp/server-fs", "/tmp"]},
            "remote": {"type": "http", "url": "https://example.test/mcp"},
        }
    }
    servers = parse_mcp_servers(cfg)
    assert [s.name for s in servers] == ["filesystem", "remote"]
    assert servers[0].command == "npx -y @mcp/server-fs /tmp"
    assert servers[1].command == "https://example.test/mcp"


def test_mcp_servers_from_vscode_style_config():
    cfg = {"mcp": {"servers": {"local": {"command": "uvx", "args": ["thing"]}}}}
    servers = parse_mcp_servers(cfg)
    assert len(servers) == 1
    assert servers[0].name == "local"
    assert servers[0].command == "uvx thing"


def test_mcp_servers_missing_key_yields_empty_list():
    assert parse_mcp_servers({}) == []


def test_tool_dump_accepts_flat_list():
    tools = [
        {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
        {"name": "fetch", "description": "Fetch", "inputSchema": {"type": "object"}},
    ]
    parsed = parse_tool_dump(tools)
    assert list(parsed) == ["(unnamed)"]
    assert [t["name"] for t in parsed["(unnamed)"]] == ["search", "fetch"]


def test_tool_dump_accepts_tools_wrapper_and_server_map():
    wrapped = {"tools": [{"name": "a"}]}
    assert parse_tool_dump(wrapped)["(unnamed)"][0]["name"] == "a"

    per_server = {"fs": {"tools": [{"name": "read"}]}, "git": [{"name": "log"}]}
    parsed = parse_tool_dump(per_server)
    assert sorted(parsed) == ["fs", "git"]
    assert parsed["git"][0]["name"] == "log"


def test_tool_dump_rejects_non_json_shapes():
    for bad in ("a string", 3, None):
        try:
            parse_tool_dump(bad)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for %r" % (bad,))


def test_tool_dump_reads_from_file(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(json.dumps([{"name": "x", "inputSchema": {"a": 1}}]), encoding="utf-8")
    parsed = parse_tool_dump(json.loads(path.read_text(encoding="utf-8")))
    assert parsed["(unnamed)"][0]["name"] == "x"
