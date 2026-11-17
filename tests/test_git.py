import gc
import pathlib
import sys
import weakref

import pytest

from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
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


def test_a_failing_git_command_is_a_recommit_error(repo: Repo) -> None:
    git = repo.client
    for run in (git.run, git.run_bytes):
        with pytest.raises(RecommitError, match=r"^git rev-parse --verify nope failed:\nfatal: "):
            run("rev-parse", "--verify", "nope")


def test_resolve_commit_says_why_a_name_isnt_one(repo: Repo) -> None:
    git = repo.client
    assert git.resolve_commit("HEAD") == repo.git("rev-parse", "HEAD")
    # git's reason, but for the one it gives for any name that isn't one.
    with pytest.raises(RecommitError) as missing:
        git.resolve_commit("nope")
    assert str(missing.value) == "nope isn't a commit"
    with pytest.raises(RecommitError, match="(?s)^@{u} isn't a commit:\n.*upstream"):
        git.resolve_commit("@{u}")


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
    with pytest.raises(RecommitError, match="expected blob type"):
        git.blob(commit)
    # And objects can still be read after.
    assert git.blob(repo.git("rev-parse", "HEAD:README")) == b"readme\n"
    assert git.read_object(commit, "commit").startswith(b"tree ")


def test_a_client_no_longer_used_is_freed_at_once(repo: Repo) -> None:
    # By its reference count, which stops its cat-file process, rather than
    # whenever the cycle collector gets to it.
    git = GitClient(repo.path)
    git.head()
    # CatFile's own, as nothing else says whether its process is running.
    process = git.cat_file._process  # pyright: ignore[reportPrivateUsage]
    assert process is not None
    freed = weakref.ref(git)
    gc.disable()
    try:
        del git
        assert freed() is None
        assert process.poll() is not None
    finally:
        gc.enable()


@pytest.mark.usefixtures("fresh_find_git")
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_a_client_that_finds_no_git_is_freed_quietly(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GIT_RECOMMIT_GIT")
    with pytest.raises(RecommitError, match="git isn't in any directory on PATH"):
        GitClient()


@pytest.mark.usefixtures("fresh_find_git")
def test_runs_git_recommit_git_if_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_RECOMMIT_GIT", "/somewhere/git")
    assert GitClient().program == "/somewhere/git"


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
