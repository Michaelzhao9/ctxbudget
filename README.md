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
- Attribute the content a compaction threw away to the component that carried it (tool results, thinking, prompts, attachments).
- Measure whether the pictures in a transcript are new ones or the same screenshot placed again, by content digest and pixel area, never by base64 length.
- Reclaim SQLite space only through an explicit, verified `--apply` path.
- Reconcile git worktree registrations with the agent state keyed by those checkouts, without pruning either side.

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

## Worktree reconciliation

```bash
ctxbudget --worktrees ~/Desktop/Code
ctxbudget --worktrees ~/Desktop/Code --json
```

A checkout can disappear while two independent records of it survive: git's registration (`.git/worktrees/<id>/gitdir`, plus `.git/worktrees/<id>/locked`) and the agent state keyed by the checkout's absolute path (`~/.claude/projects/<key>`, where every character outside `[A-Za-z0-9-]` in the path becomes `-`). `--worktrees` reads both sides and reports them next to each other. It does not shell out to `git`, and it never moves, deletes, or prunes anything.

Registry entries are classified as `present`, `path_absent` (the parent directory is readable, so the checkout is gone), `unreachable` (an ancestor above the parent is missing, so the checkout may still exist), `foreign` (something else is at that path), or locked with its recorded reason. State keys are classified as `state_without_checkout` (the key resolves under a scanned worktree base and no registration mentions it) or `state_unrooted` (the repository itself is outside the scan, so only the key is evidence).

Each repository gets one verdict word, and the word never claims more than was measured: `registration_without_checkout` for a checkout that is provably gone, `unverifiable` when any registration could not be probed at all, then `state_without_checkout`, `foreign_path`, `clean`, or `no_registry`.

The encoded key cannot be decoded backwards: a hyphen may stand for a hyphen, a dot, or a slash, so every real child directory is encoded forward and compared instead of guessing the original path. The key is the fact; the reconstructed root is printed as a hint.

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

The measured side is grouped by the `trigger` each compaction receipt carries before any median is taken. A declared trigger fraction can only describe an automatic compaction, so `--ratio` compares against the `auto` group and prints the other groups beside it instead of averaging them in; two or more trigger values with no `auto` refuse the comparison (`mixed_triggers`) rather than measure a population nobody configured. When the receipts carry a single trigger value the grouping cannot move the median, and the report says so.

A median is one boundary out of the population, so `--ratio` also counts the boundaries inside the same ±tolerance band it judges the median by. A `threshold_confirmed` with `51 of 85` in the band is a statement about the middle of the distribution; the count is what says how much of the population the declaration actually describes.

Those three numbers come from three different populations and are printed as such: `boundaries` is the population the median and the band count were taken over (every receipt in the chosen trigger group, including ones with no `postTokens`), `boundaries_total` is every receipt that carries a `preTokens` at all, and `boundaries_with_post` is how many of the counted population also carry a post-compaction size. When a receipt records `preTokens` without `postTokens` the report says how many of the counted boundaries are in that position instead of quoting the median beside a pair count it was never computed from.

The report also prints the fraction a declared trigger *would* have needed to be: `measured.implied_fraction` is the measured median divided by the declared window, printed as `implied trigger: 96.7% (you declared 95.0%)`. It is a ratio, not a share, so it is never clamped — a median of 967,169 against a declared 200,000-token window prints `483.6%`, which is the same contradiction `window_below_measured` names, in the units the user typed. The declared fraction stays in `--trigger`; nothing is inferred back into the declaration.

### What was actually in the window

`--postcompact` says how much survived a compaction, `--ratio` says whether a declared window matches what was measured. Neither says what the discarded content was *made of*.

```bash
ctxbudget --components ~/.claude/projects
ctxbudget --components ~/.claude/projects --json
```

Measured on this machine (39 transcripts carrying 85 automatic compactions, 267,271,279 characters attributed):

```
- tool results        197,517,782  73.90%
- attachments          34,065,905  12.75%
- tool arguments       28,088,335  10.51%
- your prompts          5,293,697   1.98%
- assistant text        2,036,798   0.76%
- thinking                227,182   0.09%
- system reminders         41,580   0.02%

tool results by tool:
- Read                146,714,123  74.28% of tool results
- Bash                 29,673,164  15.02% of tool results
```

The numbers are **characters, not tokens**: nothing here guesses a tokens-per-character ratio, and the receipt's own `preTokens` stays the only token count in the tool. Shares are shares of counted characters.

Five rules keep the attribution honest:

- Only `user` and `assistant` records own the content they carry. A transcript also stores an `api-request-blob` that echoes a request whose blocks are already records; counting it would double every character it repeats, so those records are skipped and counted in `ignored_records`.
- A `tool_result` is named by the tool call its `tool_use_id` points at. When no call in that transcript names the id, the characters still count toward the total and are reported as unattributed instead of being given an invented tool name.
- A segment ends at each compaction boundary: content after the last boundary is the context the harness has *not* thrown away, so it is reported as `not_yet_compacted_characters` instead of being folded into the answer.
- Lines that do not parse are counted and kept out of the denominator.
- `--components` is its own mode; it refuses `--disk`, `--diff`, `--reclaim`, `--postcompact`, `--ratio`, `--measure`, `--window`, `--trigger`, `--per-turn` and `--worktrees` rather than silently choosing one question to answer.

### Whether the same file was read again

`Read` carried 74% of the compacted characters. That number says nothing about *why*: a
few huge files and one file read twenty times look identical in it. `--rereads` splits
them.

```bash
ctxbudget --rereads ~/.claude/projects
ctxbudget --rereads ~/.claude/projects --json
```

Measured on this machine (833 transcripts, 1,079 compacted `Read` results, 752 paths):

```
counted: 146,714,123 characters across 1,079 reads of 752 paths,
         41,763,710 of them repeat reads
same range re-read: 40,919,461 characters (197 reads) revisited an
         offset/limit already read — a true duplicate, not new lines
not yet compacted: 224,686,946 characters read after the last boundary,
         reported and not charged
```

Read in two populations — 448 image paths and 304 text paths:

```
- images   142,719,256 characters   97.28% of Read characters   (27.54% is re-reading)
- text       3,994,867 characters    2.72%                       (34.06% is re-reading)
```

So the honest reading is *not* "stop reading source files". Nearly all of the `Read`
weight is base64 of screenshots that were read, read again, and then compacted away;
text re-reads are 1.36M characters in total, with one Pine script read 18 times as the
largest single case. Characters are also a **bad proxy for the cost of an image**: a
model bills an image by its pixels, not by the length of its base64, so the image share
above is a share of *characters*, and this tool does not convert it to tokens anywhere.

Rules specific to this mode:

- The first charged read of a path is the cost of learning it; every later read of the
  same path is charged as a repeat. A repeat at the *same* `offset`/`limit` is reported
  separately as a true duplicate, because a re-read at a new offset is reading new lines.
- Accounting happens only when a boundary settles a segment. Content after the last
  boundary is still in context, so a duplicate found there can never make an earlier,
  genuinely compacted read look like a repeat — the recount in `scratch/r125` checks
  exactly this and disagreed by 45M characters before the fix.
- A `Read` result whose call is missing from the transcript (compacted away earlier)
  is counted as unattributed and given no path.
- `--rereads` is its own mode and refuses every other mode flag, the same way
  `--components` does.

### Whether the pictures are new ones or the same one placed again

97% of the characters charged to compacted `Read` results on this machine are base64
image payloads, and base64 length is not a size. Two different situations were being
counted as one: *one screenshot pasted twenty times* (stop re-pasting) and *twenty
different screenshots* (use pictures less). `--images` separates them by hashing the
picture's bytes for identity and reading the pixel size out of the image header.

```bash
ctxbudget --images ~/.claude/projects
ctxbudget --images ~/.claude/projects --top 3     # 3 rows per ranking
ctxbudget --images ~/.claude/projects --json      # every row, never truncated
```

Measured on this machine (2,453 image placements in 120 transcripts, 6.2 s):

```
images: 2,453 placements of 2,153 distinct pictures in 120 transcripts
- the same picture placed again: 300 placements, 235,884,260 of
  2,452,309,126 pixels (9.62%)
  - twice inside one transcript: 41
  - an extra copy in another transcript: 259; 118 pictures appear in more
    than one transcript
- pasted into a prompt (not returned by a tool): 48 placements,
  34,725,882 pixels, 1 of them a picture already in that context
- charged to a compaction: 845 placements of 829 distinct pictures
  (16 placements, 25,287,780 pixels beyond the first copy), 166,904,656 characters
```

The answer reverses the previous run's advice. Re-pasting the same screenshot is worth
**9.6% of image pixels, and 2.9% inside the compacted subset**; the cost is the number of
*distinct* pictures (2,153, mean ≈ 1.1 megapixels). Not re-reading a screenshot saves
little; taking fewer, smaller screenshots is what moves this number. That is a
measurement, not an estimate, and it is the opposite of what the characters suggested.

Three counting rules keep the figures honest:

- **Order-free repeats.** "The second time" depends on which file a directory walk reads
  first, so the tool never attributes a repeat to a particular file: it reports
  `placements - distinct_images`, per-transcript repeats as `count - 1` summed per
  transcript, and the remainder as extra copies in other transcripts. Renaming the files
  cannot change any number.
- **`api-request-blob` records are copies.** Those records replay a request body; their
  images are counted and then skipped (439 placements, 98,676,532 characters here), never
  charged twice.
- **Pixels, not tokens.** Images are billed by the provider's own rule, so this tool reads
  width × height from the image header (PNG, JPEG, GIF) and never converts pixels to a
  token count. Anything it cannot decode (webp, or a header past the first 128 KiB of
  base64) is counted as a placement with no pixels and reported as unknown size.

`--top N` is the same flag the default scan uses for its source list: with `--images` it
sets how many rows each of the three rankings prints (default 8), and the hidden tail is
still counted on the line below, so a shorter list never hides how much was left out
(`--top 3` here prints `2,150 more pictures`, `117 more transcripts`, `68 more sessions`;
the totals do not move). It is refused with `--images --json` instead of being silently
ignored, because the JSON carries the full ranking by design.

`--images` is its own mode and refuses every other mode flag. It counts every tool that
returned an image, while `--rereads` counts `Read` results only — so the compacted subsets
of the two commands are close but not equal (845 placements here vs. 1,079 `Read` results
there, of which 591 carry an image).

### Which transcripts held the pixels

A total of 2.45 billion pixels says nothing you can act on; the per-picture list says the
expensive pictures are one-off 2000-px screenshots. The second list ranks the transcripts
themselves, because a session that holds 10% of every image on the machine is a different
decision from "use smaller screenshots".

```
transcripts that held the most pixels (pixels, not tokens; share of every pixel in this scan):
- ...-cd8eedb4fa42.jsonl   241 placements  240 pictures  248,766,898 px  10.1%
- ...-d0cdabe2bc7c.jsonl    60 placements   56 pictures  123,129,020 px   5.0%
```

(Session names truncated here; the tool prints each transcript's path relative to the
scanned root, shortened from the left with `...` when it is longer than the column.)

Measured here across 120 transcripts with pictures (6.7 s): the heaviest single transcript
holds **10.1%** of all image pixels in 241 placements of 240 distinct pictures, and the top
eight together hold **38.0%**. That is a small number of long sessions, not a uniform habit —
and it is also why "the average session" is not a useful target for advice.

Transcript rows use this transcript's own counts: pixels are what its placements added up
to, and `repeat_pixels` is that row's pixels minus the pixels of the distinct pictures it
holds. A picture that appears once in each of two transcripts is therefore a repeat in
neither row (each one placed it once) even though the global repeat count sees two
placements of one picture — the two numbers answer different questions and are never added
together. The per-transcript rows sum exactly to the reported totals, which is what makes
the ranking safe to read.

### The same pixels grouped by conversation

Ranking by file answers "which transcript", but a conversation is not a file. A resumed
session writes `<session-id>.jsonl` and then `<session-id>/subagents/agent-*.jsonl`, so one
conversation can arrive as eight files and sit low in the per-file list. The same rows are
therefore folded by the session id the path carries, and the block is printed only when at
least one conversation arrived as more than one file:

```
the same pixels grouped by session id (a resumed conversation writes more than one file, so the per-file ranking splits it):
- cbf5db94      8 file(s)   306 placements  300 pictures   324,618,838 px   13.2%
- e938bbba      5 file(s)   206 placements  202 pictures   220,423,962 px    9.0%
- ...
- 63 more session(s) with pictures
- 20 session(s) hold more than one file and 68.3% of the pixels
- 50.4% of those pixels sit in subagent transcripts (49 file(s)); a subagent runs its own
  context, so a session row is what one conversation cost, not what one window held
```

Measured here (120 files with pictures → 71 conversations): the heaviest single file holds
10.1% of all image pixels and the same conversation holds 13.2% once its 8 files are joined;
the top eight conversations hold 59.1%. A transcript whose name carries no session id is
its own row rather than being pooled with its neighbours, so no file drops out of the fold:
`sum(files)` over the rows equals the number of transcript rows, and the pixels in the rows
sum to the scan total.

One thing the row does not claim: a subagent transcript is a separate context window, so a
session row is the cost filed under one conversation, not the contents of one window. That
is why the subagent share is printed next to the fold instead of being left implicit.


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

The suite covers context discovery and pricing, duplicate detection, selectors and budget exits, disk receipts and diffs, redaction, SQLite soft-delete inspection and verified reclaim, retention reporting, compaction receipts, declared-versus-measured window comparisons, per-component attribution of what a compaction discarded, and worktree/state reconciliation (real `git worktree` fixtures, including a deleted checkout and a locked one).

## Limitations

- Client configuration and receipt formats change; unknown shapes are reported, not guessed.
- The fallback token estimator is approximate.
- Near-duplicate detection measures textual overlap, not semantic equivalence.
- Redaction cannot protect sensitive text inside file contents or filenames.
- Retention checks use file modification time and may differ from a product's internal policy.
- Compaction and window readers currently understand Claude Code receipt shapes.
- Disk size labels use binary divisors; exact byte counts are included where relevant.
- `--images` reads PNG, JPEG and GIF headers; webp and truncated payloads are reported as unknown size, and pixels are never converted to tokens because the conversion belongs to the provider.
- `--images` prints two rankings: per file, and folded by the session id in the path (printed only when a conversation spans files). Grouping is by id alone, so on this machine 120 files fold to 71 conversations and 20 of them hold 68.3% of the pixels; a (directory, id) key reports 90 conversations and 10 multi-file ones, because it splits a session's main file from its `subagents/` directory. Subagent transcripts run in their own context and are still counted inside their conversation — the block states that share (50.4% here) rather than hiding it.
- `--worktrees` reconciles only repositories reachable from the scanned path; state whose repository is outside that path is reported as unrooted, never as orphaned.
- In `--worktrees`, the agent-state key is authoritative and the reconstructed checkout path is a hint: the encoding is not reversible when a path contains hyphens, dots, or spaces.

## License

MIT — see [LICENSE](LICENSE).
