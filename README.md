# ctxbudget

Measure the hidden context and local storage cost of AI coding-agent stacks.

Agent clients inject instruction files, skill descriptions, and MCP tool schemas into every turn. They also accumulate transcripts, rollouts, and SQLite state on disk. `ctxbudget` makes both costs visible without starting untrusted MCP servers or uploading local data.

## Features

- Attribute per-turn context cost to instruction files, skills, and MCP tool schemas.
- Compare estimated counts with exact `tiktoken` counts when available.
- Find near-duplicate always-loaded sources.
- Show what removing a skill, source, or MCP server would save.
- Enforce a token budget in CI.
- Audit known agent data stores without following symlinks.
- Rank the largest files and top-level directories.
- Compare two disk receipts to show growth over time.
- Inspect SQLite soft-deleted rows and reclaimable space.
- Report retention schedules and files already past their claimed window.
- Read Claude Code compaction receipts and compare declared versus measured windows.
- Reclaim SQLite space only through an explicit, verified `--apply` path.

## Install

Requires Python 3.9+.

```bash
git clone https://github.com/Michaelzhao9/ctxbudget.git
cd ctxbudget
uv venv
uv pip install .
```

Optional exact tokenizer:

```bash
uv pip install tiktoken
```

Without `tiktoken`, every report labels counts as an estimate.

## Context audit

```bash
ctxbudget .
ctxbudget . --json
ctxbudget . --global
ctxbudget . --mcp-tools tools.json
ctxbudget . --mcp-config .mcp.json
ctxbudget . --top 10
```

Example using the synthetic fixture included in this repository:

```bash
ctxbudget examples/demo-project \
  --mcp-tools examples/demo-project/mcp-tools.json
```

The report separates sources loaded every turn from skill bodies loaded only on demand.

### What would removing a source save?

```bash
ctxbudget . --mcp-tools tools.json --without mcp:github
```

Selectors may target `mcp:<server>`, `skill:<name>`, `kind:<kind>`, `path:<substring>`, or a label substring. Unknown selectors fail with exit code 2 instead of silently measuring nothing.

### CI gate

```bash
ctxbudget . --mcp-tools tools.json --max-tokens 4000
```

Exit codes: `0` under budget, `1` over budget, `2` invalid input.

## Disk audit

```bash
ctxbudget --disk
ctxbudget --disk --json
ctxbudget --disk --items 10
ctxbudget --disk --by-project 5
ctxbudget --disk --soft-delete
ctxbudget --disk --retention
ctxbudget --disk --redact
```

Known stores include Claude Code transcripts, Codex sessions and databases, OpenCode state, and Orca sessions. Missing stores are reported as not found rather than counted as zero.

`--redact` replaces the home path and project-directory names before a report is shared. It does not inspect file contents and cannot remove sensitive text embedded in a filename.

### Compare two scans

```bash
ctxbudget --disk --receipt before.json
# run again later
ctxbudget --disk --receipt after.json
ctxbudget --diff before.json after.json --top 10
```

The comparison refuses incompatible receipt types and reports when a redacted home prevents machine-identity validation.

## Compaction and declared windows

```bash
ctxbudget --postcompact ~/.claude/projects --per-turn 600

ctxbudget --ratio \
  --window 1000000 \
  --trigger 0.9 \
  --per-turn 600 \
  --measure ~/.claude/projects
```

`--postcompact` reads only compaction metadata fields. `--ratio` requires a declared window, prints both sides of each ratio, and refuses to infer a model window from its name.

## SQLite reclaim

Dry-run is the default:

```bash
ctxbudget --reclaim /path/to/store.db
```

Apply only after reviewing the plan:

```bash
ctxbudget --reclaim /path/to/store.db --apply \
  --receipt reclaim-receipt.md
```

The write path refuses recent or live databases and non-SQLite files. It creates a sibling compacted copy, verifies `integrity_check`, table row counts, and schema fingerprint, `fsync`s the copy, then atomically replaces the original. A failed verification leaves the original untouched.

## MCP schema input

`ctxbudget` never launches an MCP server. Export schemas with a client you already trust and pass the JSON file:

```bash
mcp-cli list --format json > tools.json
ctxbudget . --mcp-tools tools.json
```

Accepted forms include a tool list, `{"tools": [...]}`, and per-server tool lists. A declared server with no schema dump is shown as unpriced, not zero-cost.

## Counting model

When `tiktoken` is unavailable, the fallback estimator is:

- non-CJK: `max(chars / 4, words * 4 / 3)`
- CJK: one additional token per CJK character

Every human and JSON report includes the tokenizer name and whether the result is estimated.

## Safety and privacy

- Audits are local and do not modify the stores they inspect. `--receipt` writes the requested receipt file.
- Symlinks are not followed during store scans.
- MCP servers are never executed.
- `--reclaim --apply` replaces the target database; without `--apply`, reclaim is read-only.
- No credentials, prompts, transcripts, or machine-specific measurements are included in this repository.
- Examples and tests use synthetic fixtures.

## Tests

```bash
uv run --with pytest pytest -q
```

The suite covers context discovery and pricing, duplicate detection, selectors and budget exits, disk receipts and diffs, redaction, SQLite soft-delete inspection and verified reclaim, retention reporting, compaction receipts, and declared-versus-measured window comparisons.

## Limitations

- Client configuration and receipt formats change; unknown shapes are reported, not guessed.
- The fallback token estimator is approximate.
- Near-duplicate detection measures textual overlap, not semantic equivalence.
- Redaction cannot protect sensitive text inside file contents or filenames.
- Retention checks use file modification time and may differ from a product's internal policy.
- Compaction and window readers currently understand Claude Code receipt shapes.
- Disk size labels use binary divisors; exact byte counts are included where relevant.

## License

MIT — see [LICENSE](LICENSE).
