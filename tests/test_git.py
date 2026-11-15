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
    index, plan = git.git_paths("index", "recommit/plan.json")
    assert (index, plan) == (str(repo.path / ".git" / "index"), str(repo.path / ".git" / "recommit" / "plan.json"))
    # Asked again, in another order, with one not asked for before.
    assert git.git_paths("MERGE_HEAD", "index") == [str(repo.path / ".git" / "MERGE_HEAD"), index]


def test_reading_an_object_of_another_kind_is_refused(repo: Repo) -> None:
    git = repo.client
    commit = repo.git("rev-parse", "HEAD")
    with pytest.raises(RecommitError, match="isn't a blob"):
        git.blob(commit)
    # And objects can still be read after.
    assert git.blob(repo.git("rev-parse", "HEAD:README")) == b"readme\n"
    assert git.read_object(commit, "commit").startswith(b"tree ")
