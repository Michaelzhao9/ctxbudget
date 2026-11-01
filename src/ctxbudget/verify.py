"""Structural fingerprints for SQLite databases.

A reclaim is only safe if the smaller file still holds every row. This module
records what a database *contains* — integrity verdict, row count per table and
a hash of the schema — so a compacted copy can be compared against the original
before it replaces anything.

Read-only: connections are opened with ``mode=ro`` and nothing is written.
"""

import hashlib
import os
import sqlite3

SQLITE_MAGIC = b"SQLite format 3\x00"

#: SQLite's own bookkeeping tables; not user data, never counted.
INTERNAL_PREFIX = "sqlite_"


def _connect_readonly(path):
    uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
    return sqlite3.connect(uri, uri=True)


def _table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [name for (name,) in rows if not name.startswith(INTERNAL_PREFIX)]


def _schema_digest(conn):
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(("\x1f".join("" if part is None else str(part) for part in row)).encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def fingerprint(path) -> dict:
    """Return integrity verdict, per-table row counts and a schema hash.

    Raises ``ValueError`` when the file is missing or is not a SQLite database.
    """
    path = os.path.abspath(os.path.expanduser(str(path)))
    try:
        with open(path, "rb") as handle:
            header = handle.read(len(SQLITE_MAGIC))
    except OSError as exc:
        raise ValueError("cannot read %s: %s" % (path, exc))
    if header != SQLITE_MAGIC:
        raise ValueError("%s is not a SQLite database" % path)

    conn = _connect_readonly(path)
    try:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        tables = {}
        for name in _table_names(conn):
            quoted = name.replace('"', '""')
            tables[name] = conn.execute('SELECT COUNT(*) FROM "%s"' % quoted).fetchone()[0]
        schema_sha256 = _schema_digest(conn)
    except sqlite3.Error as exc:
        raise ValueError("cannot inspect %s: %s" % (path, exc))
    finally:
        conn.close()

    integrity = "; ".join(str(row[0]) for row in integrity_rows) or "unknown"
    return {
        "path": path,
        "integrity": integrity,
        "tables": tables,
        "total_rows": sum(tables.values()),
        "schema_sha256": schema_sha256,
    }


def compare_fingerprints(before, after) -> dict:
    """Compare two fingerprints. ``ok`` is False for any lost row or schema drift."""
    issues = []
    if after["integrity"] != "ok":
        issues.append("integrity check after compaction: %s" % after["integrity"])
    if before["schema_sha256"] != after["schema_sha256"]:
        issues.append("schema changed")
    missing = sorted(set(before["tables"]) - set(after["tables"]))
    added = sorted(set(after["tables"]) - set(before["tables"]))
    for name in missing:
        issues.append("table lost: %s (%d rows)" % (name, before["tables"][name]))
    for name in added:
        issues.append("table appeared: %s (%d rows)" % (name, after["tables"][name]))
    for name in sorted(set(before["tables"]) & set(after["tables"])):
        if before["tables"][name] != after["tables"][name]:
            issues.append(
                "row count changed for %s: %d -> %d"
                % (name, before["tables"][name], after["tables"][name])
            )
    return {
        "ok": not issues,
        "issues": issues,
        "rows_before": before["total_rows"],
        "rows_after": after["total_rows"],
        "schema_sha256": after["schema_sha256"],
    }
