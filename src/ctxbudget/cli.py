"""Command line entry point."""

import argparse
import json
import os
import sys

from .diff import diff_receipts, load_receipt
from .compact import attach_turn_cost, scan_compactions
from .discover import Scan, scan
from .disk import (
    audit_stores,
    largest_items,
    reclaim_apply,
    reclaim_plan,
    rollup_by_project,
    scan_soft_delete,
)
from .dupes import find_duplicates
from .parse import parse_tool_dump, read_json
from .ratio import measurement_from_compactions, ratio_payload, render_ratio_markdown
from .retention import plan_retention
from .worktrees import render_worktrees_markdown, scan_worktrees
from .report import (
    disk_payload,
    reclaim_payload,
    render_compact_markdown,
    render_diff_markdown,
    render_disk_markdown,
    render_json,
    render_markdown,
    render_receipt_markdown,
    render_reclaim_markdown,
)

VERSION = "0.12.0"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ctxbudget",
        description=(
            "Price the context your agent stack injects on every turn: instruction "
            "files, skill descriptions and MCP tool schemas. Read-only; never starts a "
            "server and never calls the network."
        ),
    )
    parser.add_argument("path", nargs="?", default=".", help="project root to scan (default: .)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument(
        "--mcp-tools",
        metavar="FILE",
        help="JSON dump of MCP tool definitions ({server: [tools]} or a flat list)",
    )
    parser.add_argument(
        "--mcp-config",
        action="append",
        default=[],
        metavar="FILE",
        help="extra MCP config to read (repeatable)",
    )
    parser.add_argument(
        "--global",
        dest="include_global",
        action="store_true",
        help="also count user-level instruction files (e.g. ~/.claude/CLAUDE.md)",
    )
    parser.add_argument(
        "--without",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="re-price with a source removed: mcp:<server>, skill:<name>, kind:<kind>, "
        "path:<substring>, or a bare label substring (repeatable)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        metavar="N",
        help="exit 1 when the per-turn total exceeds N (CI gate)",
    )
    parser.add_argument("--top", type=int, default=None, metavar="N", help="show N sources")
    parser.add_argument(
        "--no-dupes", action="store_true", help="skip near-duplicate detection"
    )
    parser.add_argument(
        "--disk",
        action="store_true",
        help="audit the on-disk size of local agent stores under --home (read-only)",
    )
    parser.add_argument(
        "--home",
        metavar="DIR",
        default=None,
        help="home directory to inspect with --disk (default: $HOME)",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="with --disk: hide the home path and replace project directories with p1, p2 ...",
    )
    parser.add_argument(
        "--items",
        type=int,
        default=None,
        metavar="N",
        help="with --disk: also rank the N largest files inside each store",
    )
    parser.add_argument(
        "--by-project",
        type=int,
        default=None,
        metavar="N",
        help=(
            "with --disk: rank the N largest top-level directories across stores "
            "(the encoded project path for Claude/Orca, a year for Codex)"
        ),
    )
    parser.add_argument(
        "--soft-delete",
        action="store_true",
        help=(
            "with --disk: read every SQLite store and report tables that mark rows "
            "deleted (deleted_at, is_deleted, ...) instead of removing them"
        ),
    )
    parser.add_argument(
        "--retention",
        action="store_true",
        help=(
            "with --disk: report each store's deletion schedule (read from config or "
            "the product default), whether the app exposes it, and how many files are "
            "already older than it"
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        metavar="N",
        help="with --retention: the period this report claims to describe (default: 30)",
    )
    parser.add_argument(
        "--reclaim",
        metavar="DB",
        default=None,
        help="plan a SQLite reclaim for DB (dry-run unless --apply)",
    )
    parser.add_argument(
        "--diff",
        nargs=2,
        metavar=("OLD", "NEW"),
        default=None,
        help=(
            "compare two --disk receipts (JSON) and rank stores and "
            "directories by byte growth; --top N limits the directory rows"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="with --reclaim: vacuum into a copy, verify, then atomically swap it in",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=24.0,
        metavar="H",
        help="refuse --reclaim when the database changed within H hours (default: 24)",
    )
    parser.add_argument(
        "--receipt",
        metavar="FILE",
        default=None,
        help="write a reclaim receipt to FILE (.md for markdown, anything else for JSON)",
    )
    parser.add_argument(
        "--postcompact",
        metavar="DIR",
        default=None,
        help=(
            "read the harness's own compaction receipts under DIR (e.g. "
            "~/.claude/projects) and report what survived each boundary"
        ),
    )
    parser.add_argument(
        "--per-turn",
        type=int,
        default=None,
        metavar="N",
        help=(
            "with --postcompact: state N per-turn tokens (from a project scan) as a "
            "share of the median surviving window; with --ratio: use N instead of "
            "scanning PATH"
        ),
    )
    parser.add_argument(
        "--ratio",
        action="store_true",
        help=(
            "compare a declared context window (and optional compaction trigger) with "
            "the compaction receipts; needs --window"
        ),
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        metavar="TOKENS",
        help="with --ratio: the context window you declare the stack has to fit in",
    )
    parser.add_argument(
        "--trigger",
        type=float,
        default=None,
        metavar="FRACTION",
        help=(
            "with --ratio: the fraction of the window you believe triggers compaction "
            "(e.g. 0.8), compared against the measured preTokens"
        ),
    )
    parser.add_argument(
        "--measure",
        metavar="DIR",
        default=None,
        help=(
            "with --ratio: read compaction receipts under DIR for the effective "
            "trigger this client actually used"
        ),
    )
    parser.add_argument(
        "--worktrees",
        action="store_true",
        help=(
            "reconcile git worktree registrations under PATH with the agent state "
            "keyed by those checkouts; read-only, never prunes (own mode)"
        ),
    )
    parser.add_argument("--version", action="version", version="ctxbudget " + VERSION)
    return parser


def _skill_name(path):
    parts = path.replace(os.sep, "/").split("/")
    if "SKILL.md" in parts:
        index = parts.index("SKILL.md")
        if index > 0:
            return parts[index - 1]
    return None


def _matches(source, selector):
    if selector.startswith("mcp:"):
        name = selector[4:]
        return source.kind == "mcp-schema" and source.label.startswith("mcp:%s/" % name)
    if selector.startswith("skill:"):
        name = selector[6:]
        return source.kind.startswith("skill-") and _skill_name(source.path) == name
    if selector.startswith("kind:"):
        return source.kind == selector[5:]
    if selector.startswith("path:"):
        needle = selector[5:]
        return needle in source.path.replace(os.sep, "/")
    return selector in source.label or selector in source.path


def _available_selectors(scan_result):
    options = ["mcp:%s" % s.name for s in scan_result.servers]
    for source in scan_result.sources:
        if source.kind == "skill-description":
            name = _skill_name(source.path)
            if name:
                options.append("skill:%s" % name)
    options.extend(sorted({s.kind for s in scan_result.sources}))
    return options


def _filter(scan_result, selectors):
    sources = list(scan_result.sources)
    servers = list(scan_result.servers)
    removed = []
    total_before = scan_result.per_turn_tokens

    for selector in selectors:
        matched = [s for s in sources if _matches(s, selector)]
        dropped_servers = []
        if selector.startswith("mcp:"):
            name = selector[4:]
            dropped_servers = [s for s in servers if s.name == name]
            if not matched and dropped_servers:
                matched = []
        if not matched and not dropped_servers:
            raise ValueError(
                "selector %r matched nothing. Available: %s"
                % (selector, ", ".join(_available_selectors(scan_result)))
            )
        saved = sum(s.tokens for s in matched if s.per_turn)
        sources = [s for s in sources if not _matches(s, selector)]
        servers = [s for s in servers if s not in dropped_servers]
        remaining = total_before - saved
        removed.append(
            {
                "selector": selector,
                "saved_tokens": saved,
                "per_turn_tokens": remaining,
                "share": "%.1f%%" % (100.0 * saved / total_before) if total_before else "0.0%",
            }
        )
        total_before = remaining

    filtered = Scan(
        root=scan_result.root,
        sources=sources,
        servers=servers,
        warnings=list(scan_result.warnings),
    )
    return filtered, removed


def _write_receipt(path, payload):
    """Persist a receipt. Returns an exit code on failure, None on success."""
    target = os.path.abspath(os.path.expanduser(path))
    markdown = target.lower().endswith((".md", ".markdown"))
    body = render_receipt_markdown(payload) if markdown else json.dumps(
        payload, indent=2, ensure_ascii=False
    )
    try:
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
    except OSError as exc:
        sys.stderr.write("ctxbudget: cannot write receipt %s: %s\n" % (target, exc))
        return 2
    sys.stderr.write("ctxbudget: receipt written to %s\n" % target)
    return None


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.items is not None and not args.disk:
        sys.stderr.write("ctxbudget: --items requires --disk\n")
        return 2
    if args.by_project is not None and not args.disk:
        sys.stderr.write("ctxbudget: --by-project requires --disk\n")
        return 2
    if args.soft_delete and not args.disk:
        sys.stderr.write("ctxbudget: --soft-delete requires --disk\n")
        return 2
    if args.retention and not args.disk:
        sys.stderr.write("ctxbudget: --retention requires --disk\n")
        return 2
    if args.window_days is not None and args.window_days <= 0:
        sys.stderr.write("ctxbudget: --window-days must be positive\n")
        return 2
    if args.per_turn is not None and not (args.postcompact or args.ratio):
        sys.stderr.write("ctxbudget: --per-turn requires --postcompact or --ratio\n")
        return 2
    if args.per_turn is not None and args.per_turn <= 0:
        sys.stderr.write("ctxbudget: --per-turn must be positive\n")
        return 2
    if args.worktrees:
        conflicts = [
            name
            for name, active in (
                ("--disk", args.disk),
                ("--reclaim", bool(args.reclaim)),
                ("--diff", bool(args.diff)),
                ("--postcompact", bool(args.postcompact)),
                ("--ratio", args.ratio),
                ("--receipt", bool(args.receipt)),
            )
            if active
        ]
        if conflicts:
            sys.stderr.write(
                "ctxbudget: --worktrees is its own mode and cannot be combined with %s\n"
                % ", ".join(conflicts)
            )
            return 2
    if args.ratio:
        if any([args.disk, args.reclaim, args.diff, args.postcompact]):
            sys.stderr.write(
                "ctxbudget: --ratio is its own mode and cannot be combined with "
                "--disk, --reclaim, --diff or --postcompact\n"
            )
            return 2
        if args.window is None:
            sys.stderr.write(
                "ctxbudget: --ratio requires --window TOKENS (the window is declared, "
                "never inferred)\n"
            )
            return 2
        if args.window <= 0:
            sys.stderr.write("ctxbudget: --window must be positive\n")
            return 2
        if args.trigger is not None and not 0 < args.trigger <= 1:
            sys.stderr.write(
                "ctxbudget: --trigger is a fraction of the window (0 < FRACTION <= 1)\n"
            )
            return 2
    elif args.window is not None or args.trigger is not None or args.measure:
        sys.stderr.write(
            "ctxbudget: --window, --trigger and --measure belong to --ratio\n"
        )
        return 2
    if args.measure is not None and not os.path.isdir(
        os.path.abspath(os.path.expanduser(args.measure))
    ):
        sys.stderr.write("ctxbudget: %s is not a directory\n" % args.measure)
        return 2
    if args.postcompact and (args.disk or args.reclaim or args.diff):
        sys.stderr.write(
            "ctxbudget: --postcompact is its own mode and cannot be combined with "
            "--disk, --reclaim or --diff\n"
        )
        return 2
    if args.ratio:
        per_turn = args.per_turn
        per_turn_source = "flag"
        if per_turn is None:
            scan_result = scan(
                os.path.abspath(os.path.expanduser(args.path)),
                include_global=args.include_global,
                extra_mcp_configs=args.mcp_config,
            )
            per_turn = scan_result.per_turn_tokens
            per_turn_source = "scan"
        measurement = None
        if args.measure:
            measure_root = os.path.abspath(os.path.expanduser(args.measure))
            measurement = measurement_from_compactions(scan_compactions(measure_root))
        payload = ratio_payload(
            per_turn,
            args.window,
            trigger=args.trigger,
            measurement=measurement,
            per_turn_source=per_turn_source,
        )
        output = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else render_ratio_markdown(payload)
        )
        sys.stdout.write(output + "\n")
        return 0
    if args.postcompact:
        compact_root = os.path.abspath(os.path.expanduser(args.postcompact))
        if not os.path.isdir(compact_root):
            sys.stderr.write("ctxbudget: %s is not a directory\n" % args.postcompact)
            return 2
        payload = attach_turn_cost(scan_compactions(compact_root), args.per_turn)
        output = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else render_compact_markdown(payload)
        )
        sys.stdout.write(output + "\n")
        return 0
    if args.diff:
        if args.disk or args.reclaim:
            sys.stderr.write(
                "ctxbudget: --diff compares saved receipts and cannot be combined "
                "with --disk or --reclaim\n"
            )
            return 2
        try:
            old_receipt = load_receipt(args.diff[0])
            new_receipt = load_receipt(args.diff[1])
        except ValueError as exc:
            sys.stderr.write("ctxbudget: %s\n" % exc)
            return 2
        payload = diff_receipts(
            old_receipt, new_receipt, top=10 if args.top is None else args.top
        )
        output = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else render_diff_markdown(payload)
        )
        sys.stdout.write(output + "\n")
        return 3 if payload["status"] == "refused" else 0
    if args.receipt and args.disk and os.path.abspath(
        os.path.expanduser(args.receipt)
    ).lower().endswith((".md", ".markdown")):
        sys.stderr.write(
            "ctxbudget: --receipt with --disk writes JSON (so --diff can read it "
            "later); markdown receipts are for --reclaim only\n"
        )
        return 2
    if args.redact and not args.disk:
        sys.stderr.write(
            "ctxbudget: --redact currently applies to --disk only "
            "(scan mode prints the absolute root by design)\n"
        )
        return 2

    if args.worktrees:
        worktree_root = os.path.abspath(os.path.expanduser(args.path))
        if not os.path.isdir(worktree_root):
            sys.stderr.write("ctxbudget: %s is not a directory\n" % args.path)
            return 2
        payload = scan_worktrees(worktree_root, home=args.home)
        output = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else render_worktrees_markdown(payload)
        )
        sys.stdout.write(output + "\n")
        return 0

    if args.disk:
        home = os.path.abspath(os.path.expanduser(args.home or "~"))
        stores = audit_stores(home)
        items = None if args.items is None else largest_items(stores, args.items)
        projects = None
        projects_total = None
        if args.by_project is not None:
            buckets = rollup_by_project(stores)
            projects_total = len(buckets)
            projects = buckets[: args.by_project] if args.by_project > 0 else []
        soft_delete = None
        soft_delete_stats = None
        if args.soft_delete:
            soft_delete, soft_delete_stats = scan_soft_delete(stores)
        retention = None
        retention_stats = None
        if args.retention:
            retention, retention_stats = plan_retention(
                stores, home, window_days=args.window_days
            )
        payload = disk_payload(
            stores,
            home,
            items=items,
            redact=args.redact,
            projects=projects,
            projects_total=projects_total,
            soft_delete=soft_delete,
            soft_delete_stats=soft_delete_stats,
            retention=retention,
            retention_stats=retention_stats,
        )
        output = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else render_disk_markdown(payload)
        )
        if args.receipt:
            written = _write_receipt(args.receipt, payload)
            if written is not None:
                return written
        sys.stdout.write(output + "\n")
        return 0

    if args.reclaim:
        if args.apply:
            result = reclaim_apply(args.reclaim, min_age_hours=args.min_age_hours)
        else:
            result = reclaim_plan(args.reclaim, min_age_hours=args.min_age_hours)
        payload = reclaim_payload(result, version=VERSION)
        if args.receipt:
            written = _write_receipt(args.receipt, payload)
            if written is not None:
                return written
        output = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if args.json
            else render_reclaim_markdown(payload)
        )
        sys.stdout.write(output + "\n")
        return 3 if payload["status"] == "refused" else 0

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        sys.stderr.write("ctxbudget: %s is not a directory\n" % args.path)
        return 2

    tool_dump = None
    if args.mcp_tools:
        try:
            tool_dump = parse_tool_dump(read_json(args.mcp_tools))
        except ValueError as exc:
            sys.stderr.write("ctxbudget: %s\n" % exc)
            return 2

    scan_result = scan(
        root,
        include_global=args.include_global,
        tool_dump=tool_dump,
        extra_mcp_configs=args.mcp_config,
    )

    removed = []
    if args.without:
        try:
            scan_result, removed = _filter(scan_result, args.without)
        except ValueError as exc:
            sys.stderr.write("ctxbudget: %s\n" % exc)
            return 2

    duplicates = [] if args.no_dupes else find_duplicates(scan_result.sources)

    exceeded = bool(args.max_tokens is not None and scan_result.per_turn_tokens > args.max_tokens)
    budget = {
        "max_tokens": args.max_tokens,
        "per_turn_tokens": scan_result.per_turn_tokens,
        "exceeded": exceeded,
    }

    if args.json:
        output = render_json(scan_result, duplicates, budget, removed)
    else:
        output = render_markdown(
            scan_result, duplicates, budget, removed, top=args.top
        )
    sys.stdout.write(output + "\n")
    return 1 if exceeded else 0
