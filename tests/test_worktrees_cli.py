"""CLI wiring for --worktrees: its own mode, read-only, and it never gates."""

import json
import shutil

from ctxbudget.cli import main
from ctxbudget.worktrees import encode_path

from test_worktrees import git, make_repo


def test_worktrees_json_mode_reports_counts(tmp_path, capsys):
    repo = make_repo(tmp_path)
    git(repo, "worktree", "add", str(tmp_path / "wt"), "-b", "feature")
    (tmp_path / "home").mkdir()

    code = main([str(tmp_path), "--worktrees", "--json", "--home", str(tmp_path / "home")])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mode"] == "worktrees"
    assert payload["counts"]["present"] == 1
    assert payload["verdict"] == "clean"


def test_worktrees_refuses_a_missing_directory(tmp_path, capsys):
    code = main([str(tmp_path / "nope"), "--worktrees", "--json"])

    captured = capsys.readouterr()
    assert code == 2
    assert "not a directory" in captured.err


def test_worktrees_is_its_own_mode(tmp_path, capsys):
    code = main([str(tmp_path), "--worktrees", "--disk"])

    captured = capsys.readouterr()
    assert code == 2
    assert "--worktrees" in captured.err


def test_worktrees_exits_zero_even_when_a_path_is_absent(tmp_path, capsys):
    repo = make_repo(tmp_path)
    gone = tmp_path / "wt-gone"
    git(repo, "worktree", "add", str(gone), "-b", "gone")
    shutil.rmtree(gone)
    (tmp_path / "home").mkdir()

    code = main([str(tmp_path), "--worktrees", "--json", "--home", str(tmp_path / "home")])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["counts"]["path_absent"] == 1


def test_worktrees_markdown_separates_the_two_sides_and_refuses_to_say_deleted(
    tmp_path, capsys
):
    repo = make_repo(tmp_path)
    gone = repo / ".claude" / "worktrees" / "ghost"
    base = repo / ".claude" / "worktrees"
    base.mkdir(parents=True)
    home = tmp_path / "home"
    store = home / ".claude" / "projects" / encode_path(str(gone))
    store.mkdir(parents=True)
    (store / "session.jsonl").write_bytes(b"x" * 4096)

    code = main([str(tmp_path), "--worktrees", "--home", str(home)])
    out = capsys.readouterr().out

    assert code == 0
    assert "1 of 1" not in out  # no registry entry exists, so no such sentence
    assert "state without a checkout" in out
    assert "4.0 KB" in out
    assert "unreachable, not as missing" in out
    assert "Nothing was deleted" in out


def test_worktrees_markdown_reports_unrooted_state(tmp_path, capsys):
    home = tmp_path / "home"
    root = tmp_path / "work"
    root.mkdir()
    vanished = tmp_path / "vanished-repo" / ".claude" / "worktrees" / "gone"
    store = home / ".claude" / "projects" / encode_path(str(vanished))
    store.mkdir(parents=True)
    (store / "session.jsonl").write_bytes(b"q" * 1024)

    code = main([str(root), "--worktrees", "--home", str(home)])
    out = capsys.readouterr().out

    assert code == 0
    assert "whose repository is not in this scan" in out
    assert "1.0 KB" in out
    assert "hyphens stand for" in out
    assert "Nothing was deleted" in out


def test_worktrees_markdown_counts_registered_entries_with_denominators(tmp_path, capsys):
    repo = make_repo(tmp_path)
    live = tmp_path / "wt-live"
    gone = tmp_path / "wt-gone"
    git(repo, "worktree", "add", str(live), "-b", "live")
    git(repo, "worktree", "add", str(gone), "-b", "gone")
    shutil.rmtree(gone)
    (tmp_path / "home").mkdir()

    code = main([str(tmp_path), "--worktrees", "--home", str(tmp_path / "home")])
    out = capsys.readouterr().out

    assert code == 0
    assert "1 of 2 registered worktrees present" in out
    assert "1 path absent" in out
    assert "repos scanned" in out


def test_worktrees_markdown_names_the_two_verdict_words(tmp_path, capsys):
    # The report tells the user which word means "gone" and which means
    # "could not be checked", because the two are not the same claim.
    repo = make_repo(tmp_path)
    gone = tmp_path / "wt-gone"
    git(repo, "worktree", "add", str(gone), "-b", "gone")
    shutil.rmtree(gone)
    (tmp_path / "home").mkdir()

    code = main([str(tmp_path), "--worktrees", "--home", str(tmp_path / "home")])
    out = capsys.readouterr().out

    assert code == 0
    assert "verdict: registration_without_checkout" in out
    assert "registration_without_checkout means" in out
    assert "ancestor was missing, so the checkout may still exist" in out
