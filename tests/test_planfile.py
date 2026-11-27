import os

import pytest
from git_client.testing import GitCall

from git_recommit import planfile
from git_recommit.commands import AddCommit, Assign, Edit, Target
from git_recommit.errors import RecommitError
from tests.helpers import Repo


def test_saving_and_loading_keeps_the_plan(repo: Repo) -> None:
    repo.commit("wip", a="a\n", b="b\n")
    repo.git("commit", "--quiet", "--amend", "--no-edit", "--author=Someone Else <else@example.com>")
    session = repo.session("HEAD~")
    session.restore_old_commits()
    AddCommit("second").run(session)
    Assign((Target("b"),), 2).run(session)
    Edit(1, "a", b"edited\n").run(session)
    planfile.save(session)
    assert os.path.exists(planfile.path(repo.client))

    loaded = planfile.load(repo.client)
    assert [(c.message, c.author) for c in loaded.plan.commits] == [(c.message, c.author) for c in session.plan.commits]
    assert loaded.plan.commits[0].edits == session.plan.commits[0].edits
    assert sorted(loaded.plan.assignment.items()) == sorted(session.plan.assignment.items())


def test_loading_follows_changes_to_the_working_tree(repo: Repo) -> None:
    repo.commit("wip", a="1\n2\n3\n4\n5\n6\n7\n8\n9\n")
    session = repo.session("HEAD~")
    AddCommit("one").run(session)
    Assign((Target("a", side="+", first=9, last=9),), 1).run(session)
    planfile.save(session)
    # A new line before the assigned one shifts the ids and line numbers.
    repo.write("a", "0\n1\n2\n3\n4\n5\n6\n7\n8\n9\n")
    loaded = planfile.load(repo.client)
    assigned = [
        line.text for d in loaded.files for h in d.hunks for line in h.lines if line.id in loaded.plan.assignment
    ]
    assert assigned == [b"9\n"]


def test_there_is_one_plan_at_a_time(repo: Repo) -> None:
    with pytest.raises(RecommitError, match="no recommit in progress"):
        planfile.load(repo.client)
    repo.commit("wip", a="a\n")
    planfile.save(repo.session("HEAD~"))
    assert planfile.exists(repo.client)
    planfile.remove(repo.client)
    assert not planfile.exists(repo.client)


def test_a_replaced_binary_file_survives_garbage_collection(repo: Repo) -> None:
    repo.commit("base", data=b"\0one", f="f\n")
    repo.commit("change", data=b"\0two", f="f\ng\n")
    session = repo.session("HEAD~")
    AddCommit("Stub").run(session)
    AddCommit("Real").run(session)
    Assign((Target("f"),), 1).run(session)
    Assign((Target("data"),), 2).run(session)
    Edit(1, "data", b"\0stub").run(session)
    Edit(2, "data", b"\0real").run(session)
    planfile.save(session)
    # Nothing refers to the replacements' blobs, so git can drop them.
    repo.git("gc", "--quiet", "--prune=now")
    loaded = planfile.load(repo.client)
    first, second = loaded.build()
    assert repo.git("cat-file", "blob", f"{first}:data") == "\0stub"
    assert repo.git("cat-file", "blob", f"{second}:data") == "\0real"


def test_a_subcommand_finds_every_git_path_with_one_git(repo: Repo, git_calls: list[GitCall]) -> None:
    repo.commit("wip", a="a\n")
    for args in (["start", "HEAD~"], ["show"], ["write"]):
        git_calls.clear()
        repo.recommit(*args)
        assert sum("--git-path" in call for call in git_calls) == 1, args


def test_a_mode_an_edit_gives_survives_saving_and_loading(repo: Repo) -> None:
    repo.commit("base", script="a\n")
    repo.executable("script")
    repo.commit("make executable", script="b\n")
    repo.executable("script", executable=False)
    repo.commit("make it not", script="c\n")
    session = repo.session("HEAD~2")
    session.restore_old_commits()
    saved = {commit.message: commit.edits for commit in session.plan.commits}
    assert saved["make executable"]["script"].mode == "100755"

    planfile.save(session)
    loaded = planfile.load(repo.client)

    assert {commit.message: commit.edits for commit in loaded.plan.commits} == saved
