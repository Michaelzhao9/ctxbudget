"""On-disk cost of agent state, plus a guarded SQLite reclaimer.

Two facts drive this module:

* Agent clients keep session transcripts, snapshots and event databases under the
  home directory forever. Users report tens of gigabytes (see README evidence).
* Deleting rows does not return bytes. SQLite keeps freed pages in a freelist and
  the file only shrinks after ``VACUUM``. Any "cleanup" that just deletes rows
  lies about the result, so this module measures before and after.

Safety posture: auditing is strictly read-only (no writes, symlinks not followed).
Reclaiming is opt-in, refuses databases touched within ``min_age_hours``, refuses
a database with a non-empty ``-wal`` sidecar, and only replaces the file after a
successful ``VACUUM INTO`` copy, so the original is never truncated in place.
"""

import glob

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on platforms without POSIX locks
    fcntl = None

import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .verify import compare_fingerprints, fingerprint

SQLITE_MAGIC = b"SQLite format 3\x00"
DEFAULT_MIN_AGE_HOURS = 24
SIDECARS = ("-wal", "-shm", "-journal")

#: (id, path relative to home, human label). Unknown locations are reported as
#: absent rather than guessed; per-release path churn is a known limitation.
KNOWN_STORES: Tuple[Tuple[str, str, str], ...] = (
    ("claude-projects", ".claude/projects", "Claude Code session transcripts"),
    ("codex-sessions", ".codex/sessions", "Codex session rollouts"),
    ("codex-archived", ".codex/archived_sessions", "Codex archived rollouts"),
    (
        "codex-root-db",
        ".codex/*.sqlite",
        "Codex root databases (logs, state, thread history)",
    ),
    ("opencode-db", ".local/share/opencode/opencode.db", "opencode event database"),
    ("orca-sessions", ".orca/codex-runtime-home/sessions", "Orca runtime sessions"),
)


@dataclass
class Store:
    id: str
    label: str
    path: str
    exists: bool = False
    bytes: int = 0
    files: int = 0
    newest_mtime: Optional[float] = None
    skipped_symlinks: int = 0
    #: Real paths this store resolved to: one directory, one file, or the files
    #: matched by a glob pattern (``.codex/*.sqlite``). Empty when absent.
    paths: List[str] = field(default_factory=list)

    @property
    def age_hours(self) -> Optional[float]:
        if self.newest_mtime is None:
            return None
        return max(0.0, (time.time() - self.newest_mtime) / 3600.0)


GLOB_CHARS = "*?["


def _symlink_component(path):
    """Return the first symlink in ``path`` before any glob component."""
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    drive, tail = os.path.splitdrive(absolute)
    current = drive + os.sep if absolute.startswith(os.sep) else drive
    for part in tail.split(os.sep):
        if not part:
            continue
        if any(char in part for char in GLOB_CHARS):
            break
        current = os.path.join(current, part)
        if os.path.islink(current):
            return current
    return None


def resolve_paths(root):
    """Expand a configured store path into the real paths it names.

    Directories and single files resolve to themselves; a pattern resolves to its
    non-symlink matches, sorted. Nothing is followed through a symlink.
    """
    if any(char in root for char in GLOB_CHARS):
        return sorted(path for path in glob.glob(root) if not os.path.islink(path))
    if os.path.exists(root):
        return [root]
    return []


def _usage(root):
    """Return (bytes, files, newest_mtime, skipped_symlinks) without following links."""
    total = 0
    files = 0
    newest = None
    skipped = 0
    if os.path.isfile(root):
        # A store may be a single file (``opencode.db``). os.walk yields nothing
        # for a file, which used to report a multi-gigabyte database as 0 bytes.
        if os.path.islink(root):
            return 0, 0, None, 1
        try:
            stat = os.stat(root)
        except OSError:
            return 0, 0, None, 0
        return stat.st_size, 1, stat.st_mtime, 0
    for current, dirs, names in os.walk(root, followlinks=False):
        kept = []
        for name in dirs:
            full = os.path.join(current, name)
            if os.path.islink(full):
                skipped += 1
            else:
                kept.append(name)
        dirs[:] = kept
        for name in names:
            full = os.path.join(current, name)
            try:
                if os.path.islink(full):
                    skipped += 1
                    continue
                stat = os.stat(full)
            except OSError:
                continue
            total += stat.st_size
            files += 1
            if newest is None or stat.st_mtime > newest:
                newest = stat.st_mtime
    return total, files, newest, skipped


def audit_stores(home, stores=KNOWN_STORES) -> List[Store]:
    """Measure each known store under ``home``. Never writes, never follows links."""
    home = os.path.abspath(os.path.expanduser(str(home)))
    result = []
    for store_id, relative, label in stores:
        path = os.path.join(home, relative)
        entry = Store(id=store_id, label=label, path=path)
        if _symlink_component(path) is not None:
            entry.skipped_symlinks = 1
            result.append(entry)
            continue
        paths = resolve_paths(path)
        if paths:
            entry.exists = True
            entry.paths = paths
            for real in paths:
                size, files, newest, skipped = _usage(real)
                entry.bytes += size
                entry.files += files
                entry.skipped_symlinks += skipped
                if newest is not None and (
                    entry.newest_mtime is None or newest > entry.newest_mtime
                ):
                    entry.newest_mtime = newest
        result.append(entry)
    return result


def total_bytes(stores) -> int:
    return sum(s.bytes for s in stores)


@dataclass
class Item:
    """One file inside a store, ranked by apparent size."""

    store_id: str
    label: str
    path: str
    relative: str
    bytes: int
    mtime: float

    @property
    def age_hours(self) -> float:
        return max(0.0, (time.time() - self.mtime) / 3600.0)


def store_roots(store):
    """Real paths to walk for this store, resolved once by :func:`audit_stores`."""
    if store.paths:
        return list(store.paths)
    return [store.path] if store.exists else []


def _relative_for(root, full, multi):
    if multi:
        return os.path.basename(full)
    return os.path.relpath(full, root).replace(os.sep, "/")


def largest_items(stores, limit=10) -> List[Item]:
    """Return the ``limit`` largest files across ``stores``. Read-only, no links."""
    if limit <= 0:
        return []
    found = []
    for store in stores:
        if not store.exists:
            continue
        roots = store_roots(store)
        multi = len(roots) > 1
        for root in roots:
            if os.path.isfile(root):
                try:
                    stat = os.stat(root)
                except OSError:
                    continue
                found.append(
                    Item(
                        store_id=store.id,
                        label=store.label,
                        path=root,
                        relative=os.path.basename(root),
                        bytes=stat.st_size,
                        mtime=stat.st_mtime,
                    )
                )
                continue
            for current, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
                for name in names:
                    full = os.path.join(current, name)
                    if os.path.islink(full):
                        continue
                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    found.append(
                        Item(
                            store_id=store.id,
                            label=store.label,
                            path=full,
                            relative=_relative_for(root, full, multi),
                            bytes=stat.st_size,
                            mtime=stat.st_mtime,
                        )
                    )
    found.sort(key=lambda item: (-item.bytes, item.store_id, item.relative))
    return found[:limit]


@dataclass
class ProjectBucket:
    """Bytes held by one top-level directory inside one store.

    The grouping unit is deliberately the first path component: in Claude Code
    and Orca stores that component *is* the encoded project path, while Codex
    partitions by year. Naming it a bucket (not a project) keeps the report from
    claiming a semantic link the data does not carry.
    """

    store_id: str
    label: str
    key: str
    bytes: int = 0
    files: int = 0
    newest_mtime: Optional[float] = None

    @property
    def age_hours(self) -> Optional[float]:
        if self.newest_mtime is None:
            return None
        return max(0.0, (time.time() - self.newest_mtime) / 3600.0)


def rollup_by_project(stores, limit=None) -> List[ProjectBucket]:
    """Aggregate bytes per top-level directory per store, largest first.

    A single-file store (``opencode.db``) becomes one bucket with key ``.``.
    Read-only: no content is read, symlinks are skipped, not followed.
    """
    buckets = {}
    for store in stores:
        if not store.exists:
            continue
        roots = store_roots(store)

        def add(store, key, stat):
            bucket = buckets.get((store.id, key))
            if bucket is None:
                bucket = ProjectBucket(store_id=store.id, label=store.label, key=key)
                buckets[(store.id, key)] = bucket
            bucket.bytes += stat.st_size
            bucket.files += 1
            if bucket.newest_mtime is None or stat.st_mtime > bucket.newest_mtime:
                bucket.newest_mtime = stat.st_mtime

        for root in roots:
            if os.path.isfile(root):
                try:
                    add(store, ".", os.stat(root))
                except OSError:
                    continue
                continue
            for current, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
                for name in names:
                    full = os.path.join(current, name)
                    if os.path.islink(full):
                        continue
                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    relative = os.path.relpath(full, root)
                    parts = relative.split(os.sep)
                    add(store, parts[0] if len(parts) > 1 else ".", stat)
    ordered = sorted(buckets.values(), key=lambda b: (-b.bytes, b.store_id, b.key))
    if limit is not None:
        return ordered[:limit]
    return ordered


def sqlite_stats(path) -> dict:
    """Read-only page accounting. Raises ValueError for a non-SQLite file."""
    path = os.path.abspath(str(path))
    with open(path, "rb") as handle:
        header = handle.read(len(SQLITE_MAGIC))
    if header != SQLITE_MAGIC:
        raise ValueError("%s is not a SQLite database" % path)
    uri = _readonly_uri(path)
    conn = sqlite3.connect(uri, uri=True)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_pages": freelist,
        "freelist_bytes": freelist * page_size,
        "journal_mode": journal_mode,
    }


SOFT_DELETE_COLUMNS = (
    "deleted_at",
    "deletedat",
    "deleted_ts",
    "deletetime",
    "deleted_time",
    "is_deleted",
    "isdeleted",
    "removed_at",
    "removedat",
    "is_removed",
    "removed",
    "deleted",
    "trashed",
    "is_trashed",
    "discarded",
)
SOFT_DELETE_PATTERN = re.compile(r"(deleted|removed|trashed|discarded)", re.IGNORECASE)
DEFAULT_MAX_ROWS = 100000
DEFAULT_MAX_DATABASES = 20
NUMERIC_TYPES = ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC", "BOOL")

#: Parent directories searched by ``--soft-delete``, relative to home. The fixed
#: store list above is only a subset: Codex keeps ``state_*.sqlite``,
#: ``thread_history_*.sqlite`` and ``sqlite/*.db`` beside ``sessions/``, and those
#: are exactly the databases that hold rows the app calls deleted.
SCAN_ROOTS: Tuple[Tuple[str, str], ...] = (
    ("claude-home", ".claude"),
    ("codex-home", ".codex"),
    ("opencode-home", ".local/share/opencode"),
    ("orca-home", ".orca"),
)
_STORE_RELATIVES = {store_id: relative for store_id, relative, _ in KNOWN_STORES}


@dataclass
class SoftDeleteFinding:
    """One table that records deletion in a column instead of removing rows."""

    store_id: str
    database: str
    table: str
    column: str
    flagged_rows: int
    total_rows: int
    payload_bytes: int
    capped: bool = False


def _is_sqlite(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _readonly_uri(path):
    """A ``mode=ro`` SQLite URI; ``?`` and ``#`` in the path must be escaped."""
    absolute = os.path.abspath(path).replace("?", "%3f").replace("#", "%23")
    return f"file:{absolute}?mode=ro"


def sqlite_inventory(stores, limit=DEFAULT_MAX_DATABASES, home=None):
    """``(candidates, found)``: the files to scan and how many existed.

    Extension is not trusted: a ``.db`` that is not a database is skipped and a
    database without a suffix is kept. Symlinks are never followed. A database
    inside a known store keeps that store's id; one found only under a wider
    agent root is attributed to ``<agent>-home``.

    ``found`` is the count before ``limit`` is applied, so a caller can tell "no
    database has a deletion flag" from "the scan never reached those files".
    """
    found = []
    seen = set()
    roots = []
    for store in stores:
        if store.exists:
            roots.extend((store.id, root) for root in store_roots(store))
    if home:
        roots.extend(
            (root_id, os.path.join(home, relative)) for root_id, relative in SCAN_ROOTS
        )
    for root_id, root in roots:
        if not os.path.exists(root):
            continue
        if os.path.isfile(root):
            files = [(root, os.path.basename(root))]
        else:
            files = []
            for current, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
                for name in names:
                    full = os.path.join(current, name)
                    if os.path.islink(full):
                        continue
                    files.append((full, os.path.relpath(full, root).replace(os.sep, "/")))
        for full, relative in files:
            real = os.path.realpath(full)
            if real in seen or not _is_sqlite(full):
                continue
            seen.add(real)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            found.append((root_id, full, relative, size))
    found.sort(key=lambda entry: (-entry[3], entry[0], entry[2]))
    return [entry[:3] for entry in found[:limit]], len(found)


def sqlite_candidates(stores, limit=DEFAULT_MAX_DATABASES, home=None):
    """The SQLite files a scan would open, largest first (no coverage count)."""
    return sqlite_inventory(stores, limit, home=home)[0]


def _quote(identifier):
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _pick_soft_column(columns):
    """Choose the one column that records deletion for this table, or None.

    Several columns can match (``is_deleted`` next to ``deleted_at``); counting a
    row once per matching column would inflate the result, so the highest
    preference wins and the choice is printed in the report.
    """
    names = [row[1] for row in columns]
    lowered = {name.lower(): name for name in names}
    for candidate in SOFT_DELETE_COLUMNS:
        if candidate in lowered:
            return lowered[candidate]
    for name in names:
        if SOFT_DELETE_PATTERN.search(name):
            return name
    return None


def _deleted_predicate(column):
    """True for a row whose deletion marker is set, for timestamps and flags alike.

    ``IS NOT NULL AND NOT IN (0, '')`` covers the three shapes seen in the wild:
    a NULL timestamp means live, a non-NULL timestamp means deleted, and an
    integer flag is 0 or 1. Text affinity keeps ``'0'`` in the excluded set.
    """
    quoted = _quote(column)
    return f"{quoted} IS NOT NULL AND {quoted} NOT IN (0, '')"


def _payload_expression(columns):
    """Sum of the byte length of every non-numeric column, as a SQL expression."""
    parts = []
    for row in columns:
        declared = (row[2] or "").upper()
        if any(declared.startswith(prefix) for prefix in NUMERIC_TYPES):
            continue
        quoted = _quote(row[1])
        parts.append(f"length(COALESCE(CAST({quoted} AS BLOB), x''))")
    if not parts:
        return "0"
    return " + ".join(parts)


def _bounded_count(conn, sql, params):
    return conn.execute(sql, params).fetchone()[0] or 0


def _soft_delete_in_database(store_id, path, relative, max_rows):
    """Return ``(findings, tables_with_flag)`` for one database.

    ``tables_with_flag`` counts tables whose schema records deletion, even when
    every row is live. "There is no such column" and "the column is there and
    holds nothing" are different facts, and only the first is an absence of
    evidence.
    """
    uri = _readonly_uri(path)
    conn = sqlite3.connect(uri, uri=True)
    findings = []
    tables_with_flag = 0
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (table,) in tables:
            quoted_table = _quote(table)
            columns = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            column = _pick_soft_column(columns)
            if column is None:
                continue
            tables_with_flag += 1
            predicate = _deleted_predicate(column)
            total = _bounded_count(
                conn,
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted_table} LIMIT ?)",
                (max_rows + 1,),
            )
            flagged = _bounded_count(
                conn,
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted_table} "
                f"WHERE {predicate} LIMIT ?)",
                (max_rows + 1,),
            )
            capped = flagged > max_rows or total > max_rows
            flagged = min(flagged, max_rows)
            total = min(total, max_rows)
            if flagged == 0:
                continue
            payload = _bounded_count(
                conn,
                "SELECT COALESCE(SUM(payload), 0) FROM (SELECT "
                f"({_payload_expression(columns)}) AS payload FROM {quoted_table} "
                f"WHERE {predicate} LIMIT ?)",
                (max_rows,),
            )
            findings.append(
                SoftDeleteFinding(
                    store_id=store_id,
                    database=relative,
                    table=table,
                    column=column,
                    flagged_rows=flagged,
                    total_rows=total,
                    payload_bytes=int(payload),
                    capped=capped,
                )
            )
    finally:
        conn.close()
    return findings, tables_with_flag


def _home_from_stores(stores):
    """Recover the home directory from a store path so wider roots can be walked."""
    for store in stores:
        relative = _STORE_RELATIVES.get(store.id)
        if relative and store.path.endswith(relative):
            home = store.path[: -len(relative)]
            return home.rstrip(os.sep) or os.sep
    return None


def scan_soft_delete(
    stores, max_rows=DEFAULT_MAX_ROWS, max_databases=DEFAULT_MAX_DATABASES
):
    """Read every SQLite store and report tables that keep deleted rows.

    Returns ``(findings, stats)`` where ``stats`` counts databases and tables
    inspected so a zero result can be told apart from "nothing was readable".
    """
    findings = []
    stats = {
        "databases": 0,
        "databases_skipped": 0,
        "tables_scanned": 0,
        "tables_with_flag": 0,
        "row_cap": max_rows,
        "database_cap": max_databases,
    }
    candidates, found = sqlite_inventory(
        stores, max_databases, home=_home_from_stores(stores)
    )
    stats["databases_found"] = found
    stats["databases_not_scanned"] = max(0, found - len(candidates))
    for store_id, path, relative in candidates:
        try:
            stats["databases"] += 1
            conn = sqlite3.connect(_readonly_uri(path), uri=True)
            try:
                tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            finally:
                conn.close()
            stats["tables_scanned"] += tables
            found, with_flag = _soft_delete_in_database(
                store_id, path, relative, max_rows
            )
            stats["tables_with_flag"] += with_flag
            findings.extend(found)
        except (sqlite3.Error, OSError, ValueError):
            stats["databases"] -= 1
            stats["databases_skipped"] += 1
    findings.sort(
        key=lambda f: (-f.payload_bytes, f.store_id, f.database, f.table)
    )
    return findings, stats


def find_soft_delete(stores, max_rows=DEFAULT_MAX_ROWS, max_databases=DEFAULT_MAX_DATABASES):
    """``scan_soft_delete`` without the counters."""
    return scan_soft_delete(stores, max_rows=max_rows, max_databases=max_databases)[0]


def _age_hours(path, now=None):
    now = time.time() if now is None else now
    return max(0.0, (now - os.stat(path).st_mtime) / 3600.0)


def _live_safety_reason(path, min_age_hours, now=None):
    """Return why a reclaim must stop based on state observed right now."""
    for suffix in SIDECARS:
        sidecar = path + suffix
        if os.path.exists(sidecar) and os.path.getsize(sidecar) > 0:
            return "non-empty %s sidecar is present; a live writer may own this database" % suffix
    age = _age_hours(path, now)
    if age < min_age_hours:
        return (
            "modified %.1f h ago (< %.0f h safety window); close the agent and retry later"
            % (age, min_age_hours)
        )
    return None


def reclaim_plan(path, min_age_hours=DEFAULT_MIN_AGE_HOURS, now=None) -> dict:
    """Classify a database and estimate reclaimable bytes. Read-only."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    plan = {
        "path": path,
        "status": "planned",
        "reason": "",
        "size_bytes": 0,
        "reclaimable_bytes": 0,
        "age_hours": None,
        "min_age_hours": min_age_hours,
        "page_count": 0,
        "freelist_pages": 0,
        "journal_mode": "",
    }
    if not os.path.isfile(path):
        plan.update(status="refused", reason="file does not exist")
        return plan
    plan["size_bytes"] = os.stat(path).st_size
    plan["age_hours"] = _age_hours(path, now)

    try:
        stats = sqlite_stats(path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        plan.update(status="refused", reason="cannot read as SQLite: %s" % exc)
        return plan
    plan.update(
        reclaimable_bytes=min(stats["freelist_bytes"], plan["size_bytes"]),
        page_count=stats["page_count"],
        freelist_pages=stats["freelist_pages"],
        journal_mode=stats["journal_mode"],
    )

    for suffix in SIDECARS:
        sidecar = path + suffix
        if os.path.exists(sidecar) and os.path.getsize(sidecar) > 0:
            plan.update(
                status="refused",
                reason="non-empty %s sidecar is present; a live writer may own this database"
                % suffix,
            )
            return plan

    if plan["age_hours"] < min_age_hours:
        plan.update(
            status="refused",
            reason="modified %.1f h ago (< %.0f h safety window); close the agent and retry later"
            % (plan["age_hours"], min_age_hours),
        )
        return plan
    return plan


def reclaim_apply(path, min_age_hours=DEFAULT_MIN_AGE_HOURS, now=None) -> dict:
    """Checkpoint, ``VACUUM INTO`` a copy, then atomically replace the original.

    Returns the plan with ``status`` of ``reclaimed``, ``replaced``, ``no-change`` or
    ``refused`` plus measured ``before_bytes`` / ``after_bytes`` / ``reclaimed_bytes``.
    """
    plan = reclaim_plan(path, min_age_hours=min_age_hours, now=now)
    before = plan["size_bytes"]
    plan["before_bytes"] = before
    plan["after_bytes"] = before
    plan["reclaimed_bytes"] = 0
    plan["verification"] = None
    if plan["status"] == "refused":
        return plan

    target = plan["path"]
    original_mode = stat.S_IMODE(os.stat(target).st_mode)
    if plan["reclaimable_bytes"] <= 0:
        plan["status"] = "no-change"
        plan["reason"] = "no free pages to reclaim"
        return plan

    if fcntl is None:
        plan.update(status="refused", reason="file locking is unavailable; original kept")
        return plan
    lock_handle = None
    try:
        lock_handle = open(target, "rb")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        if lock_handle is not None:
            lock_handle.close()
        plan.update(status="refused", reason="database is locked; original kept: %s" % exc)
        return plan

    try:
        before_facts = fingerprint(target)
    except ValueError as exc:
        plan.update(status="refused", reason="cannot fingerprint the original: %s" % exc)
        return plan

    reason = _live_safety_reason(target, min_age_hours, now)
    if reason:
        plan.update(status="refused", reason="safety recheck failed before write: %s" % reason)
        return plan

    tmp = target + ".vacuum-tmp"
    for stale in (tmp, tmp + "-journal"):
        if os.path.exists(stale):
            os.unlink(stale)

    try:
        conn = sqlite3.connect(target)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

        conn = sqlite3.connect(target)
        try:
            conn.execute("VACUUM INTO '%s'" % tmp.replace("'", "''"))
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        plan.update(status="refused", reason="reclaim failed, original kept: %s" % exc)
        return plan

    # Prove the copy before trusting it: rows, schema and integrity must all match.
    try:
        comparison = compare_fingerprints(before_facts, fingerprint(tmp))
    except ValueError as exc:
        os.unlink(tmp)
        plan.update(status="refused", reason="vacuumed copy is unreadable, original kept: %s" % exc)
        return plan
    if not comparison["ok"]:
        os.unlink(tmp)
        plan.update(
            status="refused",
            reason="verification failed, original kept: %s" % "; ".join(comparison["issues"]),
            verification=comparison,
        )
        return plan

    try:
        reason = _live_safety_reason(target, min_age_hours, now)
        if reason:
            os.unlink(tmp)
            plan.update(status="refused", reason="safety recheck failed before swap: %s" % reason)
            return plan
        os.chmod(tmp, original_mode)
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        plan.update(status="refused", reason="reclaim failed, original kept: %s" % exc)
        return plan

    plan["verification"] = comparison
    after = os.stat(target).st_size
    plan["after_bytes"] = after
    plan["reclaimed_bytes"] = max(0, before - after)
    if after < before:
        plan["status"] = "reclaimed"
        plan["reason"] = "vacuumed into a new file and swapped it in"
    else:
        plan["status"] = "replaced"
        plan["reason"] = "vacuumed into a new file and swapped it in; file size did not change"
    return plan
