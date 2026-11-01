"""Measurement coverage: every configured store must report the bytes it holds.

A store can be a directory, a single file, or a glob of files at an agent root.
The audit walked every store and silently reported zero for the two non-directory
shapes, so a multi-gigabyte database could be listed as "0 B" next to item
rankings that named its own files.
"""

import os
import time

from ctxbudget.disk import audit_stores, largest_items


def put(path, data, age_hours=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - age_hours * 3600
    os.utime(path, (past, past))
    return path


def test_single_file_store_reports_its_bytes(tmp_path):
    put(tmp_path / ".local/share/opencode/opencode.db", b"x" * 5000)

    stores = {store.id: store for store in audit_stores(tmp_path)}

    assert stores["opencode-db"].exists is True
    assert stores["opencode-db"].bytes == 5000
    assert stores["opencode-db"].files == 1


def test_root_level_codex_databases_are_measured(tmp_path):
    put(tmp_path / ".codex/logs_2.sqlite", b"L" * 4000)
    put(tmp_path / ".codex/state_5.sqlite", b"S" * 1000)
    put(tmp_path / ".codex/logs_2.sqlite-wal", b"W" * 300)
    put(tmp_path / ".codex/sessions/2026/rollout.jsonl", b"J" * 70)

    stores = {store.id: store for store in audit_stores(tmp_path)}
    root_db = stores["codex-root-db"]

    assert root_db.exists is True
    assert root_db.bytes == 5000
    assert root_db.files == 2
    # the sidecar is not a database and the sessions store keeps its own bytes
    assert stores["codex-sessions"].bytes == 70
    # same-file-double-count guard: the top-level dbs are not inside sessions
    ranked = [item.relative for item in largest_items(list(stores.values()), limit=5)]
    assert "logs_2.sqlite" in ranked
