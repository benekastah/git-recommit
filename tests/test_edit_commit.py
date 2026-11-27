from git_recommit.sequence_editor import mark_edit
from tests.helpers import Repo


def test_mark_edit() -> None:
    todo = "pick 1111111 one\npick 2222222 two\n\n# pick 2222222 is a comment\n"
    assert mark_edit(todo, "2222222abcdef") == "pick 1111111 one\nedit 2222222 two\n\n# pick 2222222 is a comment\n"
    assert mark_edit(todo, "3333333") is None


def test_stops_at_the_commit_then_replays_the_rest(repo: Repo) -> None:
    repo.commit("one", f="1\n")
    two = repo.commit("two", g="2\n")
    repo.commit("three", h="3\n")
    repo.recommit("--edit", "HEAD~")
    assert repo.git("rev-parse", "HEAD") == two
    assert "interactive rebase in progress" in repo.git("status")
    repo.write("g", "changed\n")
    repo.git("commit", "--quiet", "--all", "--amend", "--no-edit")
    repo.git("rebase", "--continue")
    assert repo.git("log", "--format=%s", "-3").splitlines() == ["three", "two", "one"]
    assert repo.show("HEAD", "g") == "changed\n" and repo.show("HEAD", "h") == "3\n"


def test_edits_the_root_commit(repo: Repo) -> None:
    repo.commit("two", g="2\n")
    root = repo.git("rev-list", "--max-parents=0", "HEAD")
    repo.recommit("--edit", root)
    assert repo.git("rev-parse", "HEAD") == root


def test_editing_doesnt_squash_with_autosquash_on(repo: Repo) -> None:
    repo.git("config", "rebase.autoSquash", "true")
    one = repo.commit("one", f="1\n")
    repo.commit("fixup! one", f="1\nfix\n")
    repo.commit("three", h="3\n")
    repo.recommit("--edit", "HEAD~2")
    assert repo.git("rev-parse", "HEAD") == one
    repo.git("rebase", "--continue")
    # It only stopped to edit: the fixup wasn't squashed.
    assert repo.git("log", "--format=%s", "-3").splitlines() == ["three", "fixup! one", "one"]


def test_hooks_see_the_users_environment(repo: Repo) -> None:
    """git recommit mustn't change how the user's hooks treat paths."""
    repo.commit("one", f="1\n")
    repo.commit("two", g="2\n")
    seen = repo.path.parent / "seen"
    hook = repo.path / ".git" / "hooks" / "pre-rebase"
    hook.parent.mkdir(exist_ok=True)
    # git runs a hook with its own sh, even on Windows, where the path is as_posix.
    hook.write_text(f'#!/bin/sh\necho "[$GIT_LITERAL_PATHSPECS]" > "{seen.as_posix()}"\n', newline="\n")
    hook.chmod(0o755)
    repo.recommit("--edit", "HEAD~")
    assert seen.read_text() == "[]\n"
