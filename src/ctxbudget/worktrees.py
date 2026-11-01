"""Reconcile git worktree registrations with the agent state they left behind.

Read-only. This module never creates, moves, prunes or deletes anything, and it
never shells out to git: a registration is three small files, and reading them
directly keeps the answer honest about what was actually observed.
"""

import os
from typing import Dict, List, Optional

from .disk import _usage
from .report import _human_age, _human_bytes

SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    "Library",
    ".Trash",
}

#: Bases agent clients are known to create checkouts under, relative to a repo.
WORKTREE_BASES = (".claude/worktrees", ".worktrees")

#: Directories whose encoded name is worth reading as agent state.
STATE_DIRS = (".claude/projects",)


def encode_path(path: str) -> str:
    """The agent client's directory key for an absolute path.

    Verified against five existing store keys: ``/``, ``.`` and spaces all fold
    to ``-``, so ``/a/b.c/.claude/worktrees/x`` becomes ``-a-b-c--claude-worktrees-x``.
    The mapping is lossy in the other direction: a hyphen in the key may have
    been a hyphen, a dot or a slash in the path.
    """
    absolute = os.path.abspath(os.path.expanduser(path))
    return "".join(char if (char.isalnum() or char == "-") else "-" for char in absolute)


def discover_repos(root: str, max_depth: int = 3, limit: int = 50) -> List[str]:
    """Directories under ``root`` that own a ``.git`` directory.

    A ``.git`` *file* is a linked worktree, not a repository root: its main
    repository already lists it, so counting it again would invent a repo.
    """
    root = os.path.abspath(os.path.expanduser(root))
    found: List[str] = []
    base_depth = root.rstrip(os.sep).count(os.sep)
    for current, dirs, _files in os.walk(root, followlinks=False):
        depth = current.rstrip(os.sep).count(os.sep) - base_depth
        if os.path.isdir(os.path.join(current, ".git")):
            found.append(current)
            dirs[:] = []
            if len(found) >= limit:
                break
            continue
        if depth >= max_depth:
            dirs[:] = []
            continue
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in SKIP_DIRS and not os.path.islink(os.path.join(current, name))
        )
    return found


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _registry_dir(repo: str) -> str:
    return os.path.join(repo, ".git", "worktrees")


def read_registry(repo: str) -> List[Dict[str, object]]:
    """Every worktree git has registered for ``repo``, in id order."""
    admin = _registry_dir(repo)
    if not os.path.isdir(admin):
        return []
    entries: List[Dict[str, object]] = []
    for entry_id in sorted(os.listdir(admin)):
        entry_dir = os.path.join(admin, entry_id)
        if not os.path.isdir(entry_dir):
            continue
        gitdir = _read_text(os.path.join(entry_dir, "gitdir"))
        if not gitdir:
            continue
        if not os.path.isabs(gitdir):
            gitdir = os.path.normpath(os.path.join(entry_dir, gitdir))
        head = _read_text(os.path.join(entry_dir, "HEAD")) or ""
        branch = None
        if head.startswith("ref:"):
            branch = head.split("ref:", 1)[1].strip().replace("refs/heads/", "", 1)
        elif head:
            branch = "(detached %s)" % head[:8]
        locked = os.path.exists(os.path.join(entry_dir, "locked"))
        reason = _read_text(os.path.join(entry_dir, "locked")) if locked else None
        entries.append(
            {
                "id": entry_id,
                "path": os.path.dirname(gitdir),
                "gitdir": gitdir,
                "branch": branch,
                "locked": locked,
                "locked_reason": reason,
            }
        )
    return entries


def probe(path: str) -> str:
    """Classify a path without ever conflating 'cannot see' with 'not there'.

    ``present``       - the directory exists and carries a ``.git`` file.
    ``foreign``       - something else is at that path.
    ``path_absent``   - the path is gone but its parent directory is readable,
                        so the checkout was removed rather than hidden by a
                        missing disk.
    ``unreachable``   - an ancestor above the parent is missing too: an unmounted
                        volume, an ejected disk, a moved directory. The tool
                        cannot tell whether the checkout still exists.
    """
    if os.path.isdir(path):
        if os.path.isfile(os.path.join(path, ".git")):
            return "present"
        return "foreign"
    parent = os.path.dirname(path) or os.sep
    if os.path.isdir(parent):
        return "path_absent"
    ancestor = parent
    while True:
        up = os.path.dirname(ancestor)
        if up == ancestor or not up:
            break
        if os.path.isdir(up):
            return "unreachable"
        ancestor = up
    return "unreachable"


def state_store(path: str, home: str) -> Optional[Dict[str, object]]:
    """Agent state keyed by the checkout's absolute path, if any exists."""
    key = encode_path(path)
    for relative in STATE_DIRS:
        root = os.path.join(home, *relative.split("/"))
        candidate = os.path.join(root, key)
        if os.path.isdir(candidate):
            total, files, newest, _skipped = _usage(candidate)
            return {
                "key": key,
                "path": candidate,
                "bytes": total,
                "files": files,
                "newest_age_hours": (
                    None if newest is None else round(max(0.0, _now() - newest) / 3600.0, 1)
                ),
            }
    return None


def _now() -> float:
    import time

    return time.time()


def _children(base: str) -> List[str]:
    try:
        return sorted(os.listdir(base))
    except OSError:
        return []


def _state_keys(base: str, home: str) -> List[str]:
    """Agent state keys whose path lies directly under ``base``."""
    prefix = encode_path(base) + "-"
    keys = []
    for relative in STATE_DIRS:
        root = os.path.join(home, *relative.split("/"))
        for key in _children(root):
            if key.startswith(prefix) and len(key) > len(prefix):
                keys.append(key)
    return keys


def _checkout_for_key(base: str, key: str) -> Optional[str]:
    """The child of ``base`` this key names, if one exists.

    The encoded key cannot be decoded unambiguously (a hyphen may stand for a
    hyphen, a dot or a slash), so every real child is encoded forward and
    compared instead of guessing backwards.
    """
    for name in _children(base):
        if encode_path(os.path.join(base, name)) == key:
            return os.path.join(base, name)
    return None


def _store_of(key: str, home: str) -> Optional[Dict[str, object]]:
    for relative in STATE_DIRS:
        candidate = os.path.join(home, *relative.split("/"), key)
        if os.path.isdir(candidate):
            total, files, newest, _skipped = _usage(candidate)
            return {
                "key": key,
                "path": candidate,
                "bytes": total,
                "files": files,
                "newest_age_hours": (
                    None if newest is None else round(max(0.0, _now() - newest) / 3600.0, 1)
                ),
            }
    return None


def _verdict(rows: List[Dict[str, object]], orphans: List[Dict[str, object]]) -> str:
    """One word for the repo, and it must not claim more than was measured.

    A registration whose parent is readable names a checkout that is gone
    (``registration_without_checkout``). A registration behind a missing
    ancestor cannot be probed at all, so it is ``unverifiable`` - the tool
    cannot say the checkout disappeared. Unknown outranks known-absent: if
    even one row is unverifiable the repo verdict is not asserted as absent.
    """
    if any(row["state"] == "unreachable" for row in rows):
        return "unverifiable"
    if any(row["state"] == "path_absent" for row in rows):
        return "registration_without_checkout"
    if orphans:
        return "state_without_checkout"
    if any(row["state"] == "foreign" for row in rows):
        return "foreign_path"
    if rows:
        return "clean"
    return "no_registry"


def _split_worktree_key(key: str):
    """(base, encoded_root, worktree_name) for a key naming a worktree, else None."""
    for base, marker in (
        (".claude/worktrees", "--claude-worktrees-"),
        (".worktrees", "-worktrees-"),
    ):
        index = key.rfind(marker)
        if index > 0 and len(key) > index + len(marker):
            return base, key[:index], key[index + len(marker) :]
    return None


def _state_root(home: str, relative: str) -> str:
    return os.path.join(home, *relative.split("/"))


def scan_worktrees(root: str, home: Optional[str] = None) -> Dict[str, object]:
    """Read every registration and state store under ``root``. Never writes."""
    root = os.path.abspath(os.path.expanduser(root))
    home = os.path.abspath(os.path.expanduser(home or "~"))
    repos = []
    counts = {
        "repos": 0,
        "worktrees": 0,
        "present": 0,
        "path_absent": 0,
        "unreachable": 0,
        "foreign": 0,
        "locked": 0,
        "state_without_checkout": 0,
    }
    for repo in discover_repos(root):
        entries = read_registry(repo)
        rows = []
        registered = set()
        for entry in entries:
            state = probe(str(entry["path"]))
            counts[state] = counts.get(state, 0) + 1
            if entry["locked"]:
                counts["locked"] += 1
            row = dict(entry)
            row["state"] = state
            row["state_store"] = state_store(str(entry["path"]), home)
            rows.append(row)
            registered.add(os.path.abspath(str(entry["path"])))
        counts["worktrees"] += len(rows)

        bases = []
        orphans = []
        for relative in WORKTREE_BASES:
            base = os.path.join(repo, *relative.split("/"))
            if not os.path.isdir(base):
                continue
            bases.append(
                {
                    "path": relative,
                    "checkouts": len(_children(base)),
                    "state_keys": len(_state_keys(base, home)),
                }
            )
            for key in _state_keys(base, home):
                live = _checkout_for_key(base, key)
                if live is not None:
                    continue
                reconstructed = os.path.join(base, key[len(encode_path(base)) + 1 :])
                if os.path.abspath(reconstructed) in registered:
                    continue
                store = _store_of(key, home)
                if store is None:
                    continue
                store["reconstructed"] = reconstructed
                orphans.append(store)
        counts["state_without_checkout"] += len(orphans)

        if rows or orphans:
            repos.append(
                {
                    "root": repo,
                    "name": os.path.basename(repo.rstrip(os.sep)),
                    "bases": bases,
                    "worktrees": rows,
                    "orphan_state": orphans,
                    "verdict": _verdict(rows, orphans),
                }
            )
    counts["repos"] = len(repos)
    counts["repos_scanned"] = len(discover_repos(root))

    # State whose repository is not in this scan. The key is the fact; the path
    # is a hint, because every separator was folded to '-'.
    accounted = set()
    for repo in repos:
        for row in repo["worktrees"]:
            if row.get("state_store"):
                accounted.add(row["state_store"]["key"])
        for store in repo["orphan_state"]:
            accounted.add(store["key"])
    unrooted = []
    for relative in STATE_DIRS:
        state_root = _state_root(home, relative)
        for key in _children(state_root):
            if key in accounted:
                continue
            split = _split_worktree_key(key)
            if split is None:
                continue
            base, encoded_root, name = split
            plausible_root = encoded_root.replace("-", "/")
            if os.path.isdir(plausible_root):
                continue  # a repository may live there; it is just outside this scan
            store = _store_of(key, home)
            if store is None:
                continue
            store.update(
                {
                    "base": base,
                    "worktree_name": name,
                    "encoded_root": encoded_root,
                    "plausible_root": plausible_root,
                }
            )
            unrooted.append(store)
    counts["state_unrooted"] = len(unrooted)
    findings = (
        counts["path_absent"]
        + counts["unreachable"]
        + counts["foreign"]
        + counts["state_without_checkout"]
        + counts["state_unrooted"]
    )
    return {
        "mode": "worktrees",
        "root": root,
        "state_root": os.path.join(home, ".claude", "projects"),
        "repos": repos,
        "unrooted_state": unrooted,
        "counts": counts,
        "verdict": "findings"
        if findings
        else ("clean" if counts["worktrees"] else "no_worktrees"),
    }


STATE_NOTES = {
    "path_absent": "the parent directory is readable, so the checkout is not there",
    "unreachable": "an ancestor above the parent is missing; this checkout may still exist",
    "foreign": "something other than a worktree is at that path",
}


def render_worktrees_markdown(payload: Dict[str, object]) -> str:
    counts = payload["counts"]
    lines = ["# ctxbudget --worktrees", ""]
    lines.append("root            : %s" % payload["root"])
    lines.append("agent state root: %s" % payload["state_root"])
    reconciled = counts["repos"]
    lines.append(
        "repos scanned   : %d (%d with a registration or state to reconcile)"
        % (counts.get("repos_scanned", reconciled), reconciled)
    )
    lines.append("")
    if counts["worktrees"]:
        present = counts["present"]
        lines.append(
            "%d of %d registered worktrees present, %d path absent, %d unreachable, "
            "%d foreign, %d locked"
            % (
                present,
                counts["worktrees"],
                counts["path_absent"],
                counts["unreachable"],
                counts["foreign"],
                counts["locked"],
            )
        )
    state_bytes = sum(int(store.get("bytes", 0)) for repo in payload["repos"] for store in repo["orphan_state"])
    if counts["state_without_checkout"]:
        lines.append(
            "state without a checkout: %d store%s, %s"
            % (
                counts["state_without_checkout"],
                "" if counts["state_without_checkout"] == 1 else "s",
                _human_bytes(state_bytes),
            )
        )
    if not payload["repos"]:
        lines.append("no worktree registration and no worktree-keyed agent state found")
    lines.append("")

    for repo in payload["repos"]:
        lines.append("## %s" % repo["name"])
        lines.append("verdict: %s" % repo["verdict"])
        for base in repo.get("bases", []):
            lines.append(
                "base %s: %d checkout%s, %d state key%s"
                % (
                    base["path"],
                    base["checkouts"],
                    "" if base["checkouts"] == 1 else "s",
                    base["state_keys"],
                    "" if base["state_keys"] == 1 else "s",
                )
            )
        if repo["worktrees"]:
            lines.append("registry: %d entr%s" % (
                len(repo["worktrees"]),
                "y" if len(repo["worktrees"]) == 1 else "ies",
            ))
            for row in repo["worktrees"]:
                suffix = ""
                if row["locked"]:
                    suffix = " (locked: %s)" % (row["locked_reason"] or "no reason recorded")
                lines.append(
                    "  - %s  %s  branch %s%s"
                    % (row["id"], row["state"], row["branch"] or "unknown", suffix)
                )
                lines.append("    path: %s" % row["path"])
                note = STATE_NOTES.get(row["state"])
                if note:
                    lines.append("    note: %s" % note)
                store = row.get("state_store")
                if store:
                    lines.append(
                        "    state: %s in %d file%s, last write %s"
                        % (
                            _human_bytes(store["bytes"]),
                            store["files"],
                            "" if store["files"] == 1 else "s",
                            _human_age(store["newest_age_hours"]),
                        )
                    )
        else:
            lines.append("registry: no .git/worktrees entries")
        if repo["orphan_state"]:
            lines.append(
                "state without a checkout: %d" % len(repo["orphan_state"])
            )
            for store in repo["orphan_state"]:
                lines.append(
                    "  - %s  %s in %d file%s, last write %s"
                    % (
                        os.path.basename(store["reconstructed"]),
                        _human_bytes(store["bytes"]),
                        store["files"],
                        "" if store["files"] == 1 else "s",
                        _human_age(store["newest_age_hours"]),
                    )
                )
                lines.append("    key : %s" % store["key"])
                lines.append("    path: %s (not on disk)" % store["reconstructed"])
        lines.append("")

    unrooted = payload.get("unrooted_state") or []
    if unrooted:
        lines.append(
            "state under a worktree base whose repository is not in this scan: %d store%s, %s"
            % (
                len(unrooted),
                "" if len(unrooted) == 1 else "s",
                _human_bytes(sum(int(store["bytes"]) for store in unrooted)),
            )
        )
        for store in unrooted:
            lines.append(
                "  - %s  (base %s)  %s in %d file%s, last write %s"
                % (
                    store["worktree_name"],
                    store["base"],
                    _human_bytes(store["bytes"]),
                    store["files"],
                    "" if store["files"] == 1 else "s",
                    _human_age(store["newest_age_hours"]),
                )
            )
            lines.append("    key : %s" % store["key"])
            lines.append(
                "    root: %s (no directory at this path; hyphens stand for '/', '.' or "
                "'-', so treat the root as a hint and the key as the fact)"
                % store["plausible_root"]
            )
        lines.append("")

    lines.append(
        "Nothing was deleted, moved or pruned. This is a reconciliation report: a "
        "registration whose path cannot be probed is reported as unreachable, not as "
        "missing, and the decision to prune belongs to git and to you."
    )
    lines.append(
        "Repo verdicts keep the same rule: registration_without_checkout means the "
        "parent directory was read and the checkout is gone; unverifiable means an "
        "ancestor was missing, so the checkout may still exist."
    )
    return "\n".join(lines)



