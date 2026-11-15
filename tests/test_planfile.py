import os

import pytest

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
