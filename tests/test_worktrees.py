"""--worktrees: reconcile git worktree registrations with agent state stores.

Detection only. Two independent sources are read and never written:

* ``<repo>/.git/worktrees/<id>/`` - git's own registration (``gitdir``, ``HEAD``,
  optional ``locked``);
* ``~/.claude/projects/<key>`` - agent state, keyed by the checkout's absolute
  path with every character outside ``[A-Za-z0-9-]`` folded to ``-``.

The point of comparing them is the case neither side can see alone: a checkout
that no longer exists while megabytes of its session state are still on disk, or
a registration whose path cannot be probed at all. A path that cannot be probed
is reported as unreachable, never as deleted.
"""

import json
import os
import shutil
import subprocess

from ctxbudget.worktrees import encode_path, scan_worktrees


def git(cwd, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    git(repo, "init")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


def test_registered_present_worktree_is_reported_with_its_branch(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "worktree", "add", str(tmp_path / "wt-feature"), "-b", "feature")
    home = tmp_path / "home"
    home.mkdir()

    payload = scan_worktrees(str(tmp_path), home=str(home))

    assert payload["counts"]["repos"] == 1
    entries = payload["repos"][0]["worktrees"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["state"] == "present"
    assert entry["branch"] == "feature"
    assert entry["path"] == str(tmp_path / "wt-feature")
    assert payload["counts"]["present"] == 1


def test_removed_checkout_keeps_the_registration_and_is_called_path_absent(tmp_path):
    repo = make_repo(tmp_path)
    gone = tmp_path / "wt-gone"
    git(repo, "worktree", "add", str(gone), "-b", "gone")
    shutil.rmtree(gone)
    (tmp_path / "home").mkdir()

    payload = scan_worktrees(str(tmp_path), home=str(tmp_path / "home"))

    entry = payload["repos"][0]["worktrees"][0]
    assert entry["state"] == "path_absent"
    assert payload["counts"]["path_absent"] == 1
    # A missing path is not a deletion: the registration still exists and the
    # payload must not smuggle the word in.
    assert "deleted" not in json.dumps(payload).lower()


def test_registration_on_a_missing_volume_is_unreachable_not_absent(tmp_path):
    repo = make_repo(tmp_path)
    admin = repo / ".git" / "worktrees" / "external"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text("/no-such-volume-ctxbudget/wt/.git\n", encoding="utf-8")
    (admin / "HEAD").write_text("ref: refs/heads/external\n", encoding="utf-8")
    (tmp_path / "home").mkdir()

    payload = scan_worktrees(str(tmp_path), home=str(tmp_path / "home"))

    entry = payload["repos"][0]["worktrees"][0]
    assert entry["state"] == "unreachable"
    assert payload["counts"]["unreachable"] == 1


def test_locked_worktree_reports_the_reason_it_was_locked(tmp_path):
    repo = make_repo(tmp_path)
    live = tmp_path / "wt-live"
    git(repo, "worktree", "add", str(live), "-b", "live")
    git(repo, "worktree", "lock", "--reason", "on the USB stick", str(live))
    (tmp_path / "home").mkdir()

    payload = scan_worktrees(str(tmp_path), home=str(tmp_path / "home"))

    entry = payload["repos"][0]["worktrees"][0]
    assert entry["locked"] is True
    assert entry["locked_reason"] == "on the USB stick"
    assert payload["counts"]["locked"] == 1


def test_something_else_at_a_registered_path_is_foreign(tmp_path):
    repo = make_repo(tmp_path)
    live = tmp_path / "wt-live"
    git(repo, "worktree", "add", str(live), "-b", "live")
    os.remove(live / ".git")
    (tmp_path / "home").mkdir()

    payload = scan_worktrees(str(tmp_path), home=str(tmp_path / "home"))

    entry = payload["repos"][0]["worktrees"][0]
    assert entry["state"] == "foreign"
    assert payload["counts"]["foreign"] == 1


def test_state_store_without_a_checkout_is_reported_with_its_bytes(tmp_path):
    repo = make_repo(tmp_path)
    base = repo / ".claude" / "worktrees"
    base.mkdir(parents=True)
    home = tmp_path / "home"
    store = home / ".claude" / "projects" / encode_path(str(base / "ghost"))
    store.mkdir(parents=True)
    (store / "session.jsonl").write_bytes(b"x" * 2048)

    payload = scan_worktrees(str(tmp_path), home=str(home))

    orphans = payload["repos"][0]["orphan_state"]
    assert len(orphans) == 1
    assert orphans[0]["bytes"] == 2048
    assert orphans[0]["files"] == 1
    assert orphans[0]["reconstructed"] == str(base / "ghost")
    assert payload["counts"]["state_without_checkout"] == 1


def test_state_for_a_live_worktree_is_attached_to_it_not_orphaned(tmp_path):
    repo = make_repo(tmp_path)
    live = repo / ".claude" / "worktrees" / "live"
    git(repo, "worktree", "add", str(live), "-b", "live")
    home = tmp_path / "home"
    store = home / ".claude" / "projects" / encode_path(str(live))
    store.mkdir(parents=True)
    (store / "session.jsonl").write_bytes(b"y" * 512)

    payload = scan_worktrees(str(tmp_path), home=str(home))

    entry = payload["repos"][0]["worktrees"][0]
    assert entry["state"] == "present"
    assert entry["state_store"]["bytes"] == 512
    assert payload["repos"][0]["orphan_state"] == []
    assert payload["counts"]["state_without_checkout"] == 0


def test_verdict_keeps_absent_and_unverifiable_apart(tmp_path):
    # A path that is gone (parent readable) is a registration without a
    # checkout; a path the tool cannot probe at all is unverifiable. Calling
    # both "unreachable" would make the report say less than it knows.
    repo = make_repo(tmp_path / "gone-case")
    gone = tmp_path / "gone-case" / "wt-gone"
    git(repo, "worktree", "add", str(gone), "-b", "gone")
    shutil.rmtree(gone)
    (tmp_path / "gone-case" / "home").mkdir()

    absent = scan_worktrees(str(tmp_path / "gone-case"), home=str(tmp_path / "gone-case" / "home"))

    other = make_repo(tmp_path / "unprobeable-case")
    admin = other / ".git" / "worktrees" / "external"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text("/no-such-volume-ctxbudget/wt/.git\n", encoding="utf-8")
    (tmp_path / "unprobeable-case" / "home").mkdir()

    unprobeable = scan_worktrees(
        str(tmp_path / "unprobeable-case"), home=str(tmp_path / "unprobeable-case" / "home")
    )

    assert absent["repos"][0]["verdict"] == "registration_without_checkout"
    assert unprobeable["repos"][0]["verdict"] == "unverifiable"


def test_state_key_whose_repository_is_gone_is_unrooted(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "work"
    root.mkdir()
    vanished = tmp_path / "vanished-repo" / ".claude" / "worktrees" / "gone"
    store = home / ".claude" / "projects" / encode_path(str(vanished))
    store.mkdir(parents=True)
    (store / "session.jsonl").write_bytes(b"w" * 1024)

    payload = scan_worktrees(str(root), home=str(home))

    unrooted = payload["unrooted_state"]
    assert len(unrooted) == 1
    assert unrooted[0]["bytes"] == 1024
    assert unrooted[0]["key"] == encode_path(str(vanished))
    assert unrooted[0]["worktree_name"] == "gone"
    assert unrooted[0]["base"] == ".claude/worktrees"
    assert payload["counts"]["state_unrooted"] == 1
    assert payload["verdict"] == "findings"


def test_state_key_owned_by_a_scanned_repo_is_not_unrooted(tmp_path):
    repo = make_repo(tmp_path / "work")
    live = repo / ".claude" / "worktrees" / "live"
    git(repo, "worktree", "add", str(live), "-b", "live")
    home = tmp_path / "home"
    store = home / ".claude" / "projects" / encode_path(str(live))
    store.mkdir(parents=True)
    (store / "session.jsonl").write_bytes(b"v" * 128)

    payload = scan_worktrees(str(tmp_path / "work"), home=str(home))

    assert payload["unrooted_state"] == []
    assert payload["counts"]["state_unrooted"] == 0


def test_absent_registration_with_state_is_one_row_not_two(tmp_path):
    repo = make_repo(tmp_path)
    gone = repo / ".claude" / "worktrees" / "gone"
    git(repo, "worktree", "add", str(gone), "-b", "gone")
    shutil.rmtree(gone)
    home = tmp_path / "home"
    store = home / ".claude" / "projects" / encode_path(str(gone))
    store.mkdir(parents=True)
    (store / "session.jsonl").write_bytes(b"z" * 300)

    payload = scan_worktrees(str(tmp_path), home=str(home))

    entry = payload["repos"][0]["worktrees"][0]
    assert entry["state"] == "path_absent"
    assert entry["state_store"]["bytes"] == 300
    assert payload["repos"][0]["orphan_state"] == []
    assert payload["counts"]["state_without_checkout"] == 0

