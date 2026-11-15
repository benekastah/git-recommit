import pathlib

import pytest

from git_recommit.errors import RecommitError
from tests.helpers import Repo


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
