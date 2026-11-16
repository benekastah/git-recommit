import os
import pathlib
import sys
from collections.abc import Iterator

import pytest

from git_recommit.errors import RecommitError
from git_recommit.git import git_executable
from tests.helpers import GIT, Repo


def test_commit_sha_resolves_names_as_git_does(repo: Repo) -> None:
    first = repo.git("rev-parse", "HEAD")
    second = repo.commit("second", f="f\n")
    repo.git("tag", "-a", "v1", "-m", "a tag", first)
    git = repo.client
    assert git.commit_sha("HEAD") == second
    assert git.commit_sha("HEAD~") == first
    assert git.commit_sha("main") == second
    # A tag, peeled to its commit.
    assert git.commit_sha("v1") == first
    assert git.commit_sha(first[:7]) == first


def test_commit_sha_is_none_for_what_isnt_a_commit(repo: Repo) -> None:
    git = repo.client
    for rev in ("nope", "HEAD~5", "HEAD:README", "--all", "", "HEAD\nHEAD", ":/no such message"):
        assert git.commit_sha(rev) is None, rev
    # And names are still resolved after.
    assert git.commit_sha("HEAD") == repo.git("rev-parse", "HEAD")


def test_names_git_cant_resolve_are_none_quietly(repo: Repo, capfd: pytest.CaptureFixture[str]) -> None:
    git = repo.client
    tree = repo.git("rev-parse", "HEAD^{tree}")
    # No upstream, past the end of the reflog, a tree, as a commit.
    for rev in ("@{u}", "HEAD@{999}", tree):
        assert git.commit_sha(rev) is None, rev
    assert git.commit_sha("HEAD") == repo.git("rev-parse", "HEAD")
    assert capfd.readouterr().err == ""


def test_resolve_commit_says_why_a_name_isnt_one(repo: Repo) -> None:
    git = repo.client
    assert git.resolve_commit("HEAD") == repo.git("rev-parse", "HEAD")
    # git's reason, but for the one it gives for any name that isn't one.
    with pytest.raises(RecommitError) as missing:
        git.resolve_commit("nope")
    assert str(missing.value) == "nope isn't a commit"
    with pytest.raises(RecommitError, match="(?s)^@{u} isn't a commit:\n.*upstream"):
        git.resolve_commit("@{u}")


def test_reading_goes_on_after_a_name_stops_git(repo: Repo) -> None:
    git = repo.client
    # Which `git cat-file --batch-command` exits for, rather than saying it's missing.
    with pytest.raises(RecommitError, match="@{u}"):
        git.tree("@{u}")
    assert git.tree("HEAD") == repo.git("rev-parse", "HEAD^{tree}")
    assert git.blob(repo.git("rev-parse", "HEAD:README")) == b"readme\n"


def test_reading_goes_on_after_git_stopped_between_commands(repo: Repo) -> None:
    git = repo.client
    head = git.head()
    # Killed, say, while git recommit was suspended.
    process = git._cat_file  # pyright: ignore[reportPrivateUsage]
    assert process is not None
    process.kill()
    process.wait()
    assert git.head() == head


def test_commit_sha_sees_refs_that_moved_since_it_last_looked(repo: Repo) -> None:
    git = repo.client
    assert git.commit_sha("HEAD") == repo.git("rev-parse", "HEAD")
    moved = repo.commit("second", f="f\n")
    assert git.commit_sha("HEAD") == moved
    repo.git("pack-refs", "--all")
    repo.git("branch", "other", "HEAD~")
    assert git.commit_sha("other") == repo.git("rev-parse", "HEAD~")


def test_tree_is_a_commits_tree(repo: Repo) -> None:
    git = repo.client
    assert git.tree("HEAD") == repo.git("rev-parse", "HEAD^{tree}")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    assert git.tree(tree) == tree


def test_git_paths_are_where_git_keeps_them(repo: Repo) -> None:
    git = repo.client
    index, plan = map(pathlib.Path, git.git_paths("index", "recommit/plan.json"))
    assert (index, plan) == (repo.path / ".git" / "index", repo.path / ".git" / "recommit" / "plan.json")
    # Asked again, in another order, with one not asked for before.
    assert list(map(pathlib.Path, git.git_paths("MERGE_HEAD", "index"))) == [repo.path / ".git" / "MERGE_HEAD", index]


def test_reading_an_object_of_another_kind_is_refused(repo: Repo) -> None:
    git = repo.client
    commit = repo.git("rev-parse", "HEAD")
    with pytest.raises(RecommitError, match="isn't a blob"):
        git.blob(commit)
    # And objects can still be read after.
    assert git.blob(repo.git("rev-parse", "HEAD:README")) == b"readme\n"
    assert git.read_object(commit, "commit").startswith(b"tree ")


@pytest.fixture
def found_again() -> Iterator[None]:
    """git_executable finds git again, in the test and after it."""
    git_executable.cache_clear()
    yield
    git_executable.cache_clear()


@pytest.mark.usefixtures("found_again")
def test_runs_the_git_on_path_by_its_full_path(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Whatever it is (e.g. a wrapper, like git-ai's), from a directory on
    # PATH given relative to the current one; but on Windows, only a program
    # (git.exe), as Windows runs "git", not a batch file (e.g. an npm shim).
    (tmp_path / "shims").mkdir()
    (tmp_path / "shims" / "git.cmd").write_bytes(b"")
    (tmp_path / "bin").mkdir()
    wrapper = tmp_path / "bin" / ("git.exe" if sys.platform == "win32" else "git")
    wrapper.write_bytes(b"")
    wrapper.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["shims", "bin"]))
    monkeypatch.delenv("GIT_RECOMMIT_GIT")
    assert os.path.normcase(git_executable()) == os.path.normcase(wrapper)


@pytest.mark.usefixtures("found_again")
def test_runs_git_recommit_git_if_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_RECOMMIT_GIT", "/somewhere/git")
    assert git_executable() == "/somewhere/git"


@pytest.mark.skipif(sys.platform == "win32", reason="the wrapper is a shell script")
def test_every_git_process_is_git_recommit_git(repo: Repo, tmp_path: pathlib.Path) -> None:
    repo.commit("second", f="f\n")
    log = tmp_path / "log"
    wrapper = tmp_path / "git"
    wrapper.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{GIT}" "$@"\n')
    wrapper.chmod(0o755)
    hook = repo.path / ".git" / "hooks" / "commit-msg"
    hook.write_text('#!/bin/sh\necho "Change-Id: I1" >> "$1"\n')
    hook.chmod(0o755)
    # With no git on PATH, so any that doesn't go through the wrapper fails.
    (tmp_path / "empty").mkdir()
    env = {"GIT_RECOMMIT_GIT": str(wrapper), "PATH": str(tmp_path / "empty")}
    for args in (["start", "HEAD~"], ["commit", "reword", "1", "reworded"], ["write"]):
        repo.recommit(*args, env=env)
    commands = log.read_text()
    # The cat-file process and the commit-msg hook's too.
    assert "cat-file --batch-command" in commands
    assert "hook run --ignore-missing commit-msg" in commands
    assert repo.git("log", "-1", "--format=%B") == "reworded\nChange-Id: I1"
