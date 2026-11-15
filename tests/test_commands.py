import dataclasses
import random

import pytest

from git_recommit import commands
from git_recommit.commands import (
    AddCommit,
    Assign,
    Clear,
    DropEdit,
    Edit,
    MoveCommit,
    RemoveCommit,
    Restore,
    Reword,
    Target,
    resolve,
    targets_for,
)
from git_recommit.errors import RecommitError
from git_recommit.session import Session
from tests.helpers import Repo

LINES = "".join(f"{i}\n" for i in range(1, 21))


def session(repo: Repo) -> Session:
    """f: line 2 changed and line 15 changed (two hunks); g: added."""
    repo.commit("base", f=LINES)
    changed = "".join({2: "two\n", 15: "fifteen\n"}.get(i, f"{i}\n") for i in range(1, 21))
    repo.commit("change", f=changed, g="g1\ng2\n")
    return repo.session("HEAD~")


def texts(s: Session, ids: list[int]) -> list[str]:
    by_id = {line.id: line.kind + line.text.decode() for d in s.files for h in d.hunks for line in h.lines}
    return sorted(by_id[i] for i in ids)


@pytest.mark.parametrize(
    ("spec", "target"),
    [
        ("f", Target("f")),
        ("dir/a b.txt", Target("dir/a b.txt")),
        ("f:@2", Target("f", hunk=2)),
        ("f:+10", Target("f", side="+", first=10, last=10)),
        ("f:+10-14", Target("f", side="+", first=10, last=14)),
        ("f:-3-5", Target("f", side="-", first=3, last=5)),
        # Not a hunk or lines: all of it is the path.
        ("a:b", Target("a:b")),
    ],
)
def test_parsing_targets(spec: str, target: Target) -> None:
    assert Target.parse(spec) == target
    assert Target.parse(str(target)) == target


def test_resolving_targets(repo: Repo) -> None:
    s = session(repo)
    assert texts(s, resolve(s, Target("g"))) == ["+g1\n", "+g2\n"]
    assert texts(s, resolve(s, Target("f", hunk=2))) == ["+fifteen\n", "-15\n"]
    assert texts(s, resolve(s, Target("f", side="+", first=2, last=2))) == ["+two\n"]
    assert texts(s, resolve(s, Target("f", side="-", first=1, last=20))) == ["-15\n", "-2\n"]
    with pytest.raises(RecommitError, match="nope isn't changed"):
        resolve(s, Target("nope"))
    with pytest.raises(RecommitError, match="f has 2 hunks"):
        resolve(s, Target("f", hunk=3))
    with pytest.raises(RecommitError, match="no added lines 5-6 in f"):
        resolve(s, Target("f", side="+", first=5, last=6))


def test_targets_for_any_changes_resolve_to_them(repo: Repo) -> None:
    s = session(repo)
    every = [i for d in s.files for i in d.ids]
    rng = random.Random(0)
    for _ in range(50):
        ids = [i for i in every if rng.random() < 0.5]
        resolved = [i for target in targets_for(s, ids) for i in resolve(s, target)]
        assert sorted(resolved) == sorted(ids)
    # A whole file is named by its path alone.
    assert targets_for(s, resolve(s, Target("g"))) == [Target("g")]


def test_commits_are_named_by_number(repo: Repo) -> None:
    s = session(repo)
    assert AddCommit("one").run(s).commit == 1
    assert AddCommit("three").run(s).commit == 2
    result = AddCommit("two", after=1).run(s)
    assert (result.commit, result.message) == (2, "Created commit 2.")
    assert AddCommit("zero", after=0).run(s).commit == 1
    assert [c.message for c in s.plan.commits] == ["zero", "one", "two", "three"]
    MoveCommit(1, to=4).run(s)
    assert [c.message for c in s.plan.commits] == ["one", "two", "three", "zero"]
    Reword(4, "last").run(s)
    RemoveCommit(1).run(s)
    assert [c.message for c in s.plan.commits] == ["two", "three", "last"]
    with pytest.raises(RecommitError, match="there's no commit 9"):
        RemoveCommit(9).run(s)
    with pytest.raises(RecommitError, match="the message is empty"):
        Reword(1, "  \n").run(s)


def test_assigning_and_unassigning(repo: Repo) -> None:
    s = session(repo)
    AddCommit("one").run(s)
    assert Assign((Target("g"), Target("f", hunk=1)), 1).run(s).message == "Assigned to 1: 4 changes."
    assert texts(s, s.unassigned()) == ["+fifteen\n", "-15\n"]
    assert Assign((Target("g"),), None).run(s).message == "Unassigned 2 changes."
    assert texts(s, s.unassigned()) == ["+fifteen\n", "+g1\n", "+g2\n", "-15\n"]


def test_editing_a_file_in_a_commit(repo: Repo) -> None:
    s = session(repo)
    AddCommit("one").run(s)
    # The commit doesn't have g yet, so it starts from g's end state.
    assert Edit(1, "g", b"g1\ng2\n").run(s).message == "g is unchanged in commit 1."
    assert s.plan.commits[0].edits == {}
    assert "Edited g in commit 1" in Edit(1, "g", b"stub\n").run(s).message
    assert s.plan.commits[0].edits["g"].content == b"stub\n"
    assert DropEdit(1, "g").run(s).message == "Dropped commit 1's edit of g."
    with pytest.raises(RecommitError, match="commit 1 doesn't edit g"):
        DropEdit(1, "g").run(s)


def test_clearing_and_restoring(repo: Repo) -> None:
    s = session(repo)
    Restore().run(s)
    assert [c.message for c in s.plan.commits] == ["change"]
    Clear().run(s)
    assert s.plan.commits == []


def test_every_command_is_data() -> None:
    """Each command is a frozen dataclass, so it can be logged, compared and
    replayed."""
    for command in commands.ALL:
        # A frozen dataclass refuses changes with its own __setattr__.
        assert dataclasses.is_dataclass(command) and "__setattr__" in vars(command)


def test_edits_are_normalized_as_git_add_would(repo: Repo) -> None:
    s = session(repo)
    repo.git("config", "core.autocrlf", "true")
    AddCommit("one").run(s)
    # An editor that saves Windows line endings.
    Edit(1, "g", b"stub\r\n").run(s)
    assert s.plan.commits[0].edits["g"].content == b"stub\n"
