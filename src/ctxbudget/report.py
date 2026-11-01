"""Rendering of a scan: markdown for humans, JSON for machines."""

import json

from .tokens import tokenizer_info


def _thousands(n):
    return "{:,}".format(n)


def _share(tokens, total):
    if not total:
        return "0.0%"
    return "%.1f%%" % (100.0 * tokens / total)


def payload_for(scan, duplicates, budget, removed):
    return {
        "tool": "ctxbudget",
        "root": scan.root,
        "tokenizer": tokenizer_info(),
        "per_turn_tokens": scan.per_turn_tokens,
        "installed_tokens": scan.installed_tokens,
        "budget": budget,
        "sources": [
            {
                "kind": s.kind,
                "label": s.label,
                "path": s.path,
                "scope": s.scope,
                "tokens": s.tokens,
                "per_turn": s.per_turn,
                "detail": s.detail,
            }
            for s in scan.sources
        ],
        "servers": [
            {
                "name": s.name,
                "command": s.command,
                "config_path": s.config_path,
                "tool_count": s.tool_count,
                "schema_tokens": s.schema_tokens,
            }
            for s in scan.servers
        ],
        "duplicates": duplicates,
        "removed": removed,
        "warnings": scan.warnings,
    }


def render_json(scan, duplicates, budget, removed):
    return json.dumps(
        payload_for(scan, duplicates, budget, removed), indent=2, ensure_ascii=False
    )


def render_markdown(scan, duplicates, budget, removed, top=None):
    info = tokenizer_info()
    lines = []
    lines.append("# ctxbudget — per-turn context cost")
    lines.append("")
    lines.append("root: `%s`" % scan.root)
    lines.append(
        "counter: %s%s"
        % (info["name"], "  **(estimate, not a tokenizer)**" if info["estimated"] else "")
    )
    if removed:
        for entry in removed:
            lines.append(
                "without `%s`: %s tokens (-%s, %s of the total)"
                % (
                    entry["selector"],
                    _thousands(entry["per_turn_tokens"]),
                    _thousands(entry["saved_tokens"]),
                    entry["share"],
                )
            )
    lines.append("")

    total = scan.per_turn_tokens
    lines.append("## Every turn: %s tokens" % _thousands(total))
    lines.append("")
    if total:
        lines.append("| source | kind | tokens | share of per-turn |")
        lines.append("|---|---|---:|---:|")
        per_turn = scan.per_turn_sources
        shown = per_turn if top is None else per_turn[:top]
        for source in shown:
            lines.append(
                "| `%s` | %s | %s | %s |"
                % (
                    source.label,
                    source.kind,
                    _thousands(source.tokens),
                    _share(source.tokens, total),
                )
            )
        hidden = len(per_turn) - len(shown)
        if hidden > 0:
            lines.append("")
            lines.append("_%d more per-turn source(s) not shown (use --top)._ " % hidden)
    else:
        lines.append("No always-injected sources found in this tree.")

    on_demand = [s for s in scan.sources if not s.per_turn]
    lines.append("")
    lines.append(
        "## Loaded only on demand: %s tokens"
        % _thousands(sum(s.tokens for s in on_demand))
    )
    if on_demand:
        for source in on_demand:
            lines.append("- `%s` — %s tokens" % (source.label, _thousands(source.tokens)))
    else:
        lines.append("None.")

    if scan.servers:
        lines.append("")
        lines.append("## MCP servers declared")
        for server in scan.servers:
            priced = (
                "%s tools, %s tokens" % (server.tool_count, _thousands(server.schema_tokens))
                if server.tool_count
                else "schema not priced"
            )
            lines.append(
                "- `%s` — `%s` — %s" % (server.name, server.command or "(no command)", priced)
            )

    if duplicates:
        lines.append("")
        lines.append("## Near-duplicate sources")
        for dupe in duplicates:
            lines.append(
                "- `%s` ~ `%s` — %.0f%% overlap, up to %s tokens recoverable"
                % (
                    dupe["a"],
                    dupe["b"],
                    dupe["similarity"] * 100,
                    _thousands(dupe["redundant_tokens"]),
                )
            )

    if scan.warnings:
        lines.append("")
        lines.append("## Warnings")
        for warning in scan.warnings:
            lines.append("- %s" % warning)

    if budget and budget.get("max_tokens") is not None:
        lines.append("")
        if budget["exceeded"]:
            lines.append(
                "BUDGET EXCEEDED: %s > %s tokens"
                % (_thousands(budget["per_turn_tokens"]), _thousands(budget["max_tokens"]))
            )
        else:
            lines.append(
                "budget ok: %s <= %s tokens"
                % (_thousands(budget["per_turn_tokens"]), _thousands(budget["max_tokens"]))
            )

    lines.append("")
    lines.append("_%s_" % info["note"])
    return "\n".join(lines)


# --- on-disk audit (v0.2) -------------------------------------------------

import os
import time


def _human_bytes(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return "{:,} B".format(int(value))
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{:.1f} TB".format(value / 1024.0)


def _exact_size(n):
    """Human-readable size that still shows the exact byte count."""
    human = _human_bytes(n)
    if human.endswith(" B"):
        return human
    return "%s (%s B)" % (human, "{:,}".format(int(n)))


def _human_age(hours):
    if hours is None:
        return "unknown"
    if hours < 1:
        return "%.0f min ago" % (hours * 60)
    if hours < 48:
        return "%.1f h ago" % hours
    return "%.1f d ago" % (hours / 24.0)


def _dir_ordinals(payload):
    """One ordinal per directory, shared by the items and projects tables.

    Directories are ordered by the bytes this report attributes to them, so the
    biggest offender is always ``p1`` and a directory keeps one label in every
    table that mentions it — otherwise the two listings would disagree.
    """
    sizes = {}
    for item in payload.get("items") or []:
        parts = item["relative"].split("/")
        if len(parts) > 1:
            directory = "/".join(parts[:-1])
            sizes[directory] = sizes.get(directory, 0) + item["bytes"]
    for bucket in payload.get("projects") or []:
        if bucket["key"] != ".":
            sizes[bucket["key"]] = max(sizes.get(bucket["key"], 0), bucket["bytes"])
    ordered = sorted(sizes.items(), key=lambda pair: (-pair[1], pair[0]))
    return {
        directory: "p%d" % (index + 1) for index, (directory, _) in enumerate(ordered)
    }


def _redact_disk(payload, home):
    """Make a disk payload safe to paste: no home path, no project directory names.

    Directories become ``p1``, ``p2`` ... in the order they first appear in the
    (size-ranked) listing, so the biggest offender is always ``p1`` and files from
    one directory still share an ordinal. Ordinals are positions in this report,
    not hashes of the path, so nothing derived from the name can be reversed or
    dictionary-matched. File names are kept: they are what the reader needs.
    """
    home_abs = os.path.abspath(os.path.expanduser(str(home)))
    payload["home"] = "~"
    for store in payload["stores"]:
        path = store["path"]
        if path == home_abs:
            store["path"] = "~"
        elif path.startswith(home_abs + os.sep):
            store["path"] = "~" + path[len(home_abs) :].replace(os.sep, "/")
    labels = _dir_ordinals(payload)
    for item in payload.get("items") or []:
        parts = item["relative"].split("/")
        if len(parts) > 1:
            directory = "/".join(parts[:-1])
            item["relative"] = "%s/%s" % (labels[directory], parts[-1])
    for bucket in payload.get("projects") or []:
        bucket["key"] = labels.get(bucket["key"], bucket["key"])
    for finding in payload.get("soft_delete") or []:
        parts = finding["database"].split("/")
        if len(parts) > 1:
            directory = "/".join(parts[:-1])
            if directory not in labels:
                labels[directory] = "p%d" % (len(labels) + 1)
            finding["database"] = "%s/%s" % (labels[directory], parts[-1])
    for row in payload.get("retention") or []:
        path = row.get("config_path")
        if path and path != home_abs and path.startswith(home_abs + os.sep):
            row["config_path"] = "~" + path[len(home_abs) :].replace(os.sep, "/")
    payload["redacted"] = True
    payload["notes"].append(
        "paths redacted: directories are p1, p2 ... in size order, home is ~, file names kept"
    )
    return payload


def _soft_delete_coverage(payload):
    """What the scan actually reached, so a zero can be read correctly.

    The scan opens at most ``database_cap`` files, largest first, and a file that
    cannot be opened is dropped from every count. Saying only "scanned N
    databases" turns both gaps into a clean negative.
    """
    found = payload.get("soft_delete_databases_found")
    if found is None:
        return []
    opened = payload.get("soft_delete_databases", 0)
    missing = payload.get("soft_delete_databases_not_scanned", 0) or 0
    skipped = payload.get("soft_delete_databases_skipped", 0) or 0
    cap = _thousands(payload.get("soft_delete_database_cap") or 0)
    lines = []
    if missing:
        lines.append(
            "coverage: opened %s of %s SQLite file(s); %s were never read because the "
            "scan stops at the %s largest, so every count above describes only the "
            "files that were opened."
            % (
                _thousands(opened),
                _thousands(found),
                _thousands(missing),
                cap,
            )
        )
    else:
        lines.append(
            "coverage: opened every SQLite file found (%s of %s)."
            % (_thousands(opened), _thousands(found))
        )
    if skipped:
        lines.append(
            "coverage: %s SQLite file(s) could not be opened and are absent from "
            "every count above." % _thousands(skipped)
        )
    lines.append("")
    return lines


def _soft_delete_section(payload):
    """Rows an app calls deleted but never removed: three distinct states."""
    findings = payload["soft_delete"]
    databases = payload["soft_delete_databases"]
    tables = payload["soft_delete_tables_scanned"]
    lines = ["## Rows marked deleted but still stored", ""]
    if not databases:
        lines.extend(
            [
                (
                    "no SQLite store found under this home, so nothing was scanned — "
                    "**unknown**, not zero."
                ),
                "",
            ]
        )
        return lines
    if not findings:
        with_flag = payload.get("soft_delete_tables_with_flag", 0)
        if with_flag:
            lines.extend(
                [
                    "scanned %d table(s) in %d database(s); %d of them %s a "
                    "deletion flag and none has a row marked deleted — a negative "
                    "result for this moment, not an absence of the column."
                    % (tables, databases, with_flag, "carries" if with_flag == 1 else "carry"),
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "scanned %d table(s) in %d database(s) and found no "
                    "deletion-marker column — a negative result for the column "
                    "names checked (deleted, deleted_at, is_deleted, removed, "
                    "trashed, discarded)." % (tables, databases),
                    "",
                ]
            )
        lines.extend(_soft_delete_coverage(payload))
        return lines
    flagged = payload["soft_delete_flagged_rows"]
    total = sum(f["total_rows"] for f in findings)
    size = _human_bytes(payload["soft_delete_bytes"])
    lines.append(
        f"{_thousands(flagged)} of {_thousands(total)} rows across "
        f"{len(findings)} table(s) are marked deleted and still on disk — "
        f"about {size} of payload"
    )
    lines.append("")
    lines.extend(_soft_delete_coverage(payload))
    lines.append("| database | table | column | deleted rows | of rows | payload |")
    lines.append("|---|---|---|---:|---:|---:|")
    for finding in findings:
        database = finding["database"].replace("|", "\\|")
        table = finding["table"].replace("|", "\\|")
        column = finding["column"].replace("|", "\\|")
        rows = _thousands(finding["flagged_rows"])
        of_rows = _thousands(finding["total_rows"])
        payload_size = _human_bytes(finding["payload_bytes"])
        lines.append(
            f"| {database} | `{table}` | `{column}` | {rows} | {of_rows} | {payload_size} |"
        )
    lines.append("")
    if payload.get("soft_delete_capped"):
        cap = _thousands(payload.get("soft_delete_row_cap") or 0)
        lines.append(f"row counts hit the {cap} row cap; every number here is a lower bound.")
        lines.append("")
    lines.extend(
        [
            (
                "VACUUM cannot reclaim them: the pages still belong to live rows, so a "
                "free-page count reads zero. The rows must be hard-deleted first, and only "
                "then does `--reclaim --apply` return the bytes."
            ),
            "",
            (
                "Deletion-marker columns are matched by name. A schema that records deletion "
                "some other way (a separate tombstones table, a status enum) is not counted."
            ),
            "",
        ]
    )
    return lines


def disk_payload(
    stores,
    home,
    items=None,
    redact=False,
    projects=None,
    projects_total=None,
    soft_delete=None,
    soft_delete_stats=None,
    retention=None,
    retention_stats=None,
):
    payload = {
        "tool": "ctxbudget",
        "mode": "disk",
        "home": home,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_bytes": sum(s.bytes for s in stores),
        "total_files": sum(s.files for s in stores),
        "stores": [
            {
                "id": s.id,
                "label": s.label,
                "path": s.path,
                "exists": s.exists,
                "bytes": s.bytes,
                "files": s.files,
                "age_hours": None if s.age_hours is None else round(s.age_hours, 1),
                "skipped_symlinks": s.skipped_symlinks,
            }
            for s in stores
        ],
        "notes": [
            "read-only: nothing is deleted, moved or written",
            "symlinks are counted but never followed",
            "sizes are apparent bytes from stat(), not blocks on disk",
            "no encrypted or compressed container is opened",
            "item paths are relative to their store; no file contents are read",
        ],
    }
    if items is not None:
        listed = sum(item.bytes for item in items)
        total = payload["total_bytes"]
        payload["items_listed"] = len(items)
        payload["items_bytes"] = listed
        payload["items_share"] = round(listed / total, 3) if total else 0.0
        payload["items"] = [
            {
                "store_id": item.store_id,
                "label": item.label,
                "relative": item.relative,
                "bytes": item.bytes,
                "age_hours": round(item.age_hours, 1),
            }
            for item in items
        ]
    if projects is not None:
        total = payload["total_bytes"]
        listed = sum(bucket.bytes for bucket in projects)
        payload["projects_total"] = (
            len(projects) if projects_total is None else projects_total
        )
        payload["projects_listed"] = len(projects)
        payload["projects_bytes"] = listed
        payload["projects_share"] = round(listed / total, 3) if total else 0.0
        payload["projects"] = [
            {
                "store_id": bucket.store_id,
                "label": bucket.label,
                "key": bucket.key,
                "bytes": bucket.bytes,
                "files": bucket.files,
                "share": round(bucket.bytes / total, 3) if total else 0.0,
                "age_hours": (
                    None if bucket.age_hours is None else round(bucket.age_hours, 1)
                ),
            }
            for bucket in projects
        ]
    if soft_delete is not None:
        stats = soft_delete_stats or {}
        payload["soft_delete"] = [
            {
                "store_id": f.store_id,
                "database": f.database,
                "table": f.table,
                "column": f.column,
                "flagged_rows": f.flagged_rows,
                "total_rows": f.total_rows,
                "payload_bytes": f.payload_bytes,
                "capped": f.capped,
            }
            for f in soft_delete
        ]
        payload["soft_delete_databases"] = stats.get("databases", 0)
        payload["soft_delete_databases_skipped"] = stats.get("databases_skipped", 0)
        payload["soft_delete_databases_found"] = stats.get(
            "databases_found", stats.get("databases", 0)
        )
        payload["soft_delete_databases_not_scanned"] = stats.get(
            "databases_not_scanned", 0
        )
        payload["soft_delete_database_cap"] = stats.get("database_cap")
        payload["soft_delete_tables_scanned"] = stats.get("tables_scanned", 0)
        payload["soft_delete_tables_with_flag"] = stats.get("tables_with_flag", 0)
        payload["soft_delete_flagged_rows"] = sum(f.flagged_rows for f in soft_delete)
        payload["soft_delete_bytes"] = sum(f.payload_bytes for f in soft_delete)
        payload["soft_delete_capped"] = any(f.capped for f in soft_delete)
        payload["soft_delete_row_cap"] = stats.get("row_cap")
        payload["notes"].append(
            "soft-delete payload bytes are the sum of column lengths of the flagged "
            "rows, not the pages they occupy; they are an estimate of what a hard "
            "DELETE followed by VACUUM could return"
        )
    if retention is not None:
        stats = retention_stats or {}
        payload["retention_window_days"] = stats.get("window_days", 30)
        payload["retention_stores_measured"] = stats.get("stores_measured", 0)
        payload["retention_stores_known"] = stats.get("stores_known", 0)
        payload["retention_stores_unknown"] = stats.get("stores_unknown", 0)
        payload["retention_files_expired"] = stats.get("files_expired", 0)
        payload["retention_bytes_expired"] = stats.get("bytes_expired", 0)
        payload["retention"] = [
            {
                "store_id": plan.store_id,
                "label": plan.label,
                "setting": plan.setting,
                "config_path": plan.config_path,
                "configured": plan.configured,
                "days": plan.days,
                "witness": plan.witness,
                "ui_exposed": plan.ui_exposed,
                "basis": plan.basis,
                "files_expired": plan.files_expired,
                "bytes_expired": plan.bytes_expired,
                "shorter_than_window": plan.shorter_than_window,
                "verdict": plan.verdict,
            }
            for plan in retention
            if plan.exists
        ]
        payload["notes"].append(
            "a deletion schedule read from config is a claim about the product; the "
            "files older than that window are the measurement, and they are why "
            "schedule_not_in_effect is reported separately"
        )
    if redact:
        _redact_disk(payload, home)
    return payload


def _retention_total_line(payload):
    """A total is only true for as long as the vendor keeps the files it counts."""
    expired = payload.get("retention_bytes_expired") or 0
    total = payload.get("total_bytes") or 0
    if not expired or not total:
        return None
    share = round(100.0 * expired / total)
    return (
        "%s in %s file(s) — %d%% of the total — is already older than its store's "
        "retention window and is scheduled for deletion at the product's next "
        "startup — the total above is a snapshot, not a floor."
        % (
            _human_bytes(expired),
            _thousands(payload.get("retention_files_expired") or 0),
            share,
        )
    )


def _retention_section(payload):
    """Per store: which schedule was read, from where, and whether it held."""
    rows = payload.get("retention")
    if not rows:
        return []
    window = payload.get("retention_window_days") or 30
    known = payload.get("retention_stores_known") or 0
    unknown = payload.get("retention_stores_unknown") or 0
    measured = payload.get("retention_stores_measured") or 0
    lines = ["## Deletion schedule", ""]
    lines.append(
        "coverage: %d of %d store(s) have a documented deletion schedule."
        % (known, measured)
    )
    if unknown:
        lines.append(
            "%d store(s) have no documented deletion schedule — their bytes may "
            "live forever or vanish at any startup, and this report cannot tell "
            "which." % unknown
        )
    lines.append("")
    lines.append("| store | schedule | source | past the window |")
    lines.append("|---|---|---|---:|")
    for row in rows:
        label = row["label"].replace("|", "\\|")
        if row["days"] is None:
            schedule = "unknown"
            source = "no documented setting was found"
            past = "not measured"
        else:
            schedule = "%d day(s)" % row["days"]
            if row["witness"] == "config":
                source = "`%s`" % (row.get("config_path") or row["setting"])
            else:
                source = "product default, not in the settings file"
                if row.get("ui_exposed") is False:
                    source += "; no field in the app exposes it"
            if row["files_expired"]:
                past = "%s file(s) · %s" % (
                    _thousands(row["files_expired"]),
                    _human_bytes(row["bytes_expired"]),
                )
            else:
                past = "none"
        lines.append("| %s | %s | %s | %s |" % (label, schedule, source, past))
    lines.append("")
    for row in rows:
        if row["verdict"] == "schedule_not_in_effect":
            lines.append(
                "- `%s`: %s file(s) (%s) are older than the %d day(s) window and "
                "still on disk — the documented schedule did not remove them, so "
                "either it has not run or it does not use file age."
                % (
                    row["store_id"],
                    _thousands(row["files_expired"]),
                    _human_bytes(row["bytes_expired"]),
                    row["days"],
                )
            )
        if row["shorter_than_window"]:
            lines.append(
                "- `%s` keeps %d day(s), shorter than this %d day(s) report: a "
                "--diff across the full window would compare stores that were "
                "partly deleted."
                % (row["store_id"], row["days"], window)
            )
    if any(row.get("basis") for row in rows):
        lines.append("")
        for row in rows:
            if row.get("basis"):
                lines.append("- `%s` basis: %s" % (row["store_id"], row["basis"]))
    lines.append("")
    return lines


def render_disk_markdown(payload):
    present = [s for s in payload["stores"] if s["exists"]]
    missing = [s for s in payload["stores"] if not s["exists"]]
    lines = [
        "# ctxbudget — local agent stores on disk",
        "",
        "home: `%s`" % payload["home"],
        "measured: %s" % payload["measured_at"],
        "",
        "## Total: %s in %d file(s) across %d store(s)"
        % (
            _human_bytes(payload["total_bytes"]),
            payload["total_files"],
            len(present),
        ),
        "",
    ]
    qualifier = _retention_total_line(payload)
    if qualifier:
        lines.append(qualifier)
        lines.append("")
    if present:
        lines.append("| store | size | files | newest item | path |")
        lines.append("|---|---:|---:|---:|---|")
        for store in present:
            label = store["label"].replace("|", "\\|")
            lines.append(
                "| %s | %s | %d | %s | `%s` |"
                % (
                    label,
                    _exact_size(store["bytes"]),
                    store["files"],
                    _human_age(store["age_hours"]),
                    store["path"],
                )
            )
        lines.append("")
    else:
        lines.append("Nothing found: none of the known stores exist under this home.")
        lines.append("")
    if payload.get("retention"):
        lines.extend(_retention_section(payload))
    if missing:
        lines.append("Not found or refused:")
        for store in missing:
            if store["skipped_symlinks"]:
                lines.append(
                    "- %s — `%s` — refused top-level symlink; target was not traversed"
                    % (store["label"], store["path"])
                )
            else:
                lines.append("- %s — `%s`" % (store["label"], store["path"]))
        lines.append("")
    if payload.get("projects"):
        listed = payload["projects_listed"]
        groups = payload["projects_total"]
        share = (
            round(100 * payload["projects_bytes"] / payload["total_bytes"])
            if payload["total_bytes"]
            else 0
        )
        lines.append("## Largest top-level directories (top %d)" % listed)
        lines.append(
            "%d of %d directories holds %d%% of the %s total"
            % (listed, groups, share, _human_bytes(payload["total_bytes"]))
        )
        lines.append("")
        lines.append("| directory | store | size | share | files |")
        lines.append("|---|---|---:|---:|---:|")
        for bucket in payload["projects"]:
            lines.append(
                "| `%s` | %s | %s | %d%% | %d |"
                % (
                    bucket["key"].replace("|", "\\|"),
                    bucket["store_id"],
                    _exact_size(bucket["bytes"]),
                    round(100 * bucket["share"]),
                    bucket["files"],
                )
            )
        lines.append("")
    if payload.get("items"):
        lines.append("## Largest files (top %d)" % payload["items_listed"])
        lines.append(
            "covering %d%% of the %s total — the rest is spread over %d file(s)"
            % (
                round(100 * payload["items_share"]),
                _human_bytes(payload["total_bytes"]),
                max(0, payload["total_files"] - payload["items_listed"]),
            )
        )
        lines.append("")
        lines.append("| store | file (relative) | size | modified |")
        lines.append("|---|---|---:|---:|")
        for item in payload["items"]:
            lines.append(
                "| %s | `%s` | %s | %s |"
                % (
                    item["store_id"],
                    item["relative"].replace("|", "\\|"),
                    _exact_size(item["bytes"]),
                    _human_age(item["age_hours"]),
                )
            )
        lines.append("")
    if "soft_delete" in payload:
        lines.extend(_soft_delete_section(payload))
    lines.append("Notes:")
    for note in payload["notes"]:
        lines.append("- %s" % note)
    return "\n".join(lines)


def reclaim_payload(result, version=None):
    payload = {"tool": "ctxbudget", "mode": "reclaim"}
    if version:
        payload["version"] = version
        payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload.update(result)
    return payload


def _verdict(payload):
    verification = payload.get("verification")
    if payload["status"] == "refused":
        return "REFUSED — the original file was left untouched"
    if payload["status"] == "no-change":
        return "NO CHANGE — nothing was swapped"
    if verification and verification.get("ok"):
        return "VERIFIED — every row survived the compaction"
    return "UNVERIFIED — no fingerprint comparison was recorded"


def render_receipt_markdown(payload):
    """A durable record of one reclaim attempt, including refusals."""
    lines = [
        "# ctxbudget — reclaim receipt",
        "",
        "generated: %s" % payload.get("generated_at", "unknown"),
        "tool: ctxbudget %s" % payload.get("version", "(unknown)"),
        "path: `%s`" % payload["path"],
        "",
        "status: **%s**" % payload["status"],
        "size: %s -> %s (reclaimed %s)"
        % (
            _exact_size(payload.get("before_bytes", payload["size_bytes"])),
            _exact_size(payload.get("after_bytes", payload["size_bytes"])),
            _human_bytes(payload.get("reclaimed_bytes", 0)),
        ),
    ]
    verification = payload.get("verification")
    if verification:
        lines.append(
            "rows kept: %d -> %d"
            % (verification["rows_before"], verification["rows_after"])
        )
        lines.append("integrity: %s" % ("ok" if verification["ok"] else "FAILED"))
        lines.append("schema sha256: %s" % verification["schema_sha256"][:16])
        if verification["issues"]:
            lines.append("issues:")
            for issue in verification["issues"]:
                lines.append("- %s" % issue)
    else:
        lines.append("rows kept: not measured (no compaction was attempted)")
    if payload.get("reason"):
        lines.append("reason: %s" % payload["reason"])
    lines.extend(
        [
            "",
            "verdict: %s" % _verdict(payload),
            "",
            "Deleting rows does not shrink a SQLite file; only VACUUM returns free",
            "pages to the filesystem. This receipt exists so a shrink can be audited",
            "later: it records the byte counts and the row counts on both sides.",
        ]
    )
    return "\n".join(lines)


def render_reclaim_markdown(payload):
    size = payload["size_bytes"]
    reclaimable = payload["reclaimable_bytes"]
    share = (100.0 * reclaimable / size) if size else 0.0
    lines = [
        "# ctxbudget — SQLite reclaim",
        "",
        "status: **%s**" % payload["status"],
        "path: `%s`" % payload["path"],
        "size: %s" % _human_bytes(size),
        "reclaimable: %s (%.0f%% of the file)" % (_human_bytes(reclaimable), share),
        "free pages: %d of %d" % (payload["freelist_pages"], payload["page_count"]),
        "age: %s (safety window: %.0f h)"
        % (_human_age(payload["age_hours"]), payload["min_age_hours"]),
    ]
    if payload.get("after_bytes") is not None and payload["status"] != "planned":
        lines.append(
            "measured after: %s (reclaimed %s)"
            % (_human_bytes(payload["after_bytes"]), _human_bytes(payload["reclaimed_bytes"]))
        )
    if payload.get("reason"):
        lines.append("reason: %s" % payload["reason"])
    lines.extend(
        [
            "",
            "Deleting rows does not shrink a SQLite file; only VACUUM returns the",
            "free pages to the filesystem. Dry-run by default: add `--apply` to",
            "checkpoint, vacuum into a copy, fsync it and swap it in atomically.",
        ]
    )
    return "\n".join(lines)


def _signed_size(n):
    """A delta with an explicit sign, so shrinkage can never read as growth."""
    return ("+" if n >= 0 else "-") + _human_bytes(abs(n))


def _diff_span(hours):
    if hours is None:
        return "unknown (receipt timestamps missing or unparseable)"
    if hours <= 0:
        return "under 1 min (both scans landed in the same second)"
    if hours < 1:
        return "%d min" % max(0, round(hours * 60))
    if hours < 48:
        return "%.1f h" % hours
    return "%.1f d" % (hours / 24.0)


def render_diff_markdown(payload):
    if payload.get("status") == "refused":
        return "\n".join(
            [
                "# ctxbudget — refusing to diff these receipts",
                "",
                "refused: %s" % payload.get("reason", "unspecified"),
                "",
                "A delta between two different trees is not a measurement. Re-run",
                "both scans against the same home, then diff them again.",
            ]
        )

    lines = [
        "# ctxbudget — what changed between two scans",
        "",
        "home: `%s`" % payload.get("home"),
        "from: %s (%s)" % (payload["from"].get("measured_at"), _human_bytes(payload["total_before"])),
        "to:   %s (%s)" % (payload["to"].get("measured_at"), _human_bytes(payload["total_after"])),
        "span: %s" % _diff_span(payload.get("span_hours")),
    ]
    if payload.get("home_verified") is False:
        lines.extend(
            [
                "home check: **not run** — at least one receipt is redacted, so both",
                "homes read as `~`. The deltas below only make sense if both scans came",
                "from the same tree.",
            ]
        )
    lines.extend(
        [
            "",
            "## Total: %s"
            % _signed_size(payload["total_delta"]),
            "",
            "| store | before | after | change |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in payload["stores"]:
        lines.append(
            "| %s | %s | %s | %s |"
            % (
                row["label"].replace("|", "\\|"),
                _human_bytes(row["before"]),
                _human_bytes(row["after"]),
                _signed_size(row["delta"]),
            )
        )
    lines.append("")

    if payload.get("projects"):
        listed = len(payload["projects"])
        total = payload.get("projects_total", listed)
        lines.append("## Largest directories by growth (top %d of %d)" % (listed, total))
        lines.append("")
        lines.append("| directory | store | before | after | change |")
        lines.append("|---|---|---:|---:|---:|")
        for row in payload["projects"]:
            lines.append(
                "| `%s` | %s | %s | %s | %s |"
                % (
                    row["key"].replace("|", "\\|"),
                    row.get("label", row["store_id"]),
                    _human_bytes(row["before"]),
                    _human_bytes(row["after"]),
                    _signed_size(row["delta"]),
                )
            )
        lines.append("")
    elif payload.get("projects_note"):
        lines.append("Directories: %s" % payload["projects_note"])
        lines.append("")

    lines.extend(["Notes:"])
    for note in payload.get("notes", []):
        lines.append("- %s" % note)
    return "\n".join(lines)


def _pct(value):
    return "n/a" if value is None else "%.1f%%" % (100.0 * value)


def _verb(n, singular, plural):
    """Agreement for a verb next to a counted noun: '1 boundary carries' / '3 carry'."""
    return singular if n == 1 else plural


def _short_file(path, width=48):
    """Keep a transcript path readable in a table cell without losing its file name."""
    if len(path) <= width:
        return path
    head, _, base = path.rpartition("/")
    keep = width - len(base) - 6
    if not head or keep <= 0:
        return "…" + path[-(width - 1):]
    return "…" + head[-keep:] + "/" + base


def _plural(n, singular, plural=None):
    """'1 boundary' / '2 boundaries' — a receipt should not print 'boundary(ies)'."""
    if n == 1:
        return "%s %s" % (_thousands(n), singular)
    return "%s %s" % (_thousands(n), plural or singular + "s")


def render_compact_markdown(payload):
    coverage = payload["coverage"]
    totals = payload["totals"]
    survival = payload["survival"]
    lines = [
        "# ctxbudget — the window after compaction",
        "",
        "root: `%s`" % payload.get("root"),
        "coverage: %s file(s) scanned, %s with compaction receipts"
        % (_thousands(coverage["files_scanned"]), _thousands(coverage["files_with_boundaries"])),
        "source: the harness's own `compactMetadata` receipt (preTokens/postTokens/"
        "cumulativeDroppedTokens) — what the client declared, not what the model "
        "received",
        "",
        "verdict: **%s**" % payload["verdict"],
        "",
    ]

    if payload.get("boundaries"):
        lines.extend(
            [
                "| # | file | pre | post | survived | dropped (cumulative) | trigger |",
                "|---:|---|---:|---:|---:|---:|---|",
            ]
        )
        for index, row in enumerate(payload["boundaries"], start=1):
            lines.append(
                "| %d | `%s` | %s | %s | %s | %s | %s |"
                % (
                    index,
                    _short_file(row["file"]).replace("|", "\\|"),
                    _thousands(row["pre"]) if row["pre"] is not None else "absent",
                    _thousands(row["post"]) if row["post"] is not None else "absent",
                    _pct(row["survived"]),
                    _thousands(row["dropped"]) if row["dropped"] is not None else "absent",
                    row["trigger"] or "unstated",
                )
            )
        lines.append("")

    lines.append("## Totals")
    lines.append("")
    lines.append(
        "- boundaries: %s (%s carry both counts)"
        % (_thousands(totals["boundaries"]), _thousands(totals["with_pre_and_post"]))
    )
    if totals["dropped_sum"] is None:
        lines.append(
            "- cumulativeDroppedTokens: not carried by any boundary, so it is absent "
            "rather than zero"
        )
    else:
        lines.append(
            "- cumulativeDroppedTokens: %s over %s; %s %s no such field and %s "
            "not counted as zero"
            % (
                _thousands(totals["dropped_sum"]),
                _plural(totals["dropped_present"], "boundary", "boundaries"),
                _plural(totals["dropped_missing"], "boundary", "boundaries"),
                _verb(totals["dropped_missing"], "carries", "carry"),
                _verb(totals["dropped_missing"], "is", "are"),
            )
        )
    if survival["n"]:
        lines.append(
            "- survived (post/pre): median %s, min %s, max %s over %s"
            % (
                _pct(survival["median"]),
                _pct(survival["min"]),
                _pct(survival["max"]),
                _plural(survival["n"], "boundary", "boundaries"),
            )
        )
    else:
        lines.append("- survived (post/pre): refused — no boundary carries both counts")

    turn = payload.get("turn_cost")
    if turn is not None:
        lines.extend(
            [
                "",
                "## Per-turn cost inside that window",
                "",
                "- %s per-turn tokens / %s %s = **%s**"
                % (
                    _thousands(turn["per_turn"]),
                    _thousands(turn["window"]),
                    turn["window_basis"],
                    _pct(turn["share"]),
                ),
                "- the window is what survives, and it refills every turn; this is the",
                "  share of the smallest recurring budget the stack has to fit in, not",
                "  the share of a single prompt",
            ]
        )

    if payload.get("notes"):
        lines.append("")
        lines.append("Notes:")
        for note in payload["notes"]:
            lines.append("- %s" % note)
    return "\n".join(lines)
