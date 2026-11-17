import dataclasses
import os
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


def test_editing_a_renamed_file_by_its_old_path(repo: Repo) -> None:
    repo.commit("base", old=LINES)
    repo.commit("rename", old=None, new=LINES)
    s = repo.session("HEAD~")
    AddCommit("Rename").run(s)
    result = Edit(1, "old", b"edited\n").run(s)
    assert result.message == "Edited new in commit 1; later commits carry the edit."
    assert list(s.plan.commits[0].edits) == ["new"]


def test_an_edit_before_the_rename_is_cleaned_as_the_old_path(repo: Repo) -> None:
    # git add would normalize new's line endings, but not old's.
    repo.commit("base", **{".gitattributes": "new text\n", "old": LINES})
    repo.commit("rename", old=None, new=LINES)
    s = repo.session("HEAD~")
    AddCommit("Before").run(s)
    AddCommit("Rename").run(s)
    Assign((Target("new"),), 2).run(s)
    Edit(1, "new", b"crlf\r\n").run(s)
    assert s.plan.commits[0].edits["new"].content == b"crlf\r\n"


def binary_session(repo: Repo) -> Session:
    """data (binary) changed, and f (text) changed."""
    repo.commit("base", data=b"\0one", f=LINES)
    repo.commit("change", data=b"\0two", f=LINES + "21\n")
    return repo.session("HEAD~")


def test_replacing_a_binary_file_in_a_commit(repo: Repo) -> None:
    s = binary_session(repo)
    AddCommit("Stub data").run(s)
    AddCommit("Change data").run(s)
    Assign((Target("f"),), 1).run(s)
    assert Edit(1, "data", b"\0stub").run(s).message == "Edited data in commit 1; later commits carry the edit."
    first, second = s.build()
    assert repo.git("cat-file", "blob", f"{first}:data") == "\0stub"
    # Carried into the next commit, which doesn't change data.
    assert repo.git("cat-file", "blob", f"{second}:data") == "\0stub"
    assert s.left_uncommitted() == ["data"]


def test_a_replaced_binary_file_conflicts_with_a_later_change_to_it(repo: Repo) -> None:
    s = binary_session(repo)
    AddCommit("Stub data").run(s)
    AddCommit("Change data").run(s)
    Assign((Target("f"),), 1).run(s)
    Assign((Target("data"),), 2).run(s)
    Edit(1, "data", b"\0stub").run(s)
    # Both the edit and commit 2 change it, and binary files can't be merged.
    assert [(s.plan.index(c.commit.key), c.path) for c in s.conflicts()] == [(1, "data")]
    Edit(2, "data", b"\0two").run(s)
    assert s.conflicts() == []
    first, second = s.build()
    assert repo.git("cat-file", "blob", f"{first}:data") == "\0stub"
    assert repo.git("cat-file", "blob", f"{second}:data") == "\0two"


def test_replacing_a_binary_file_the_range_doesnt_change(repo: Repo) -> None:
    repo.commit("base", data=b"\0one")
    repo.commit("change", f="f\n")
    s = repo.session("HEAD~")
    AddCommit("Stub data").run(s)
    Edit(1, "data", b"\0stub").run(s)
    (sha,) = s.build()
    assert repo.git("cat-file", "blob", f"{sha}:data") == "\0stub"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_editing_a_symlink_edits_where_it_points(repo: Repo) -> None:
    (repo.path / "link").symlink_to("README")
    repo.commit("base")
    (repo.path / "link").unlink()
    (repo.path / "link").symlink_to("f")
    repo.commit("change", f="f\n")
    s = repo.session("HEAD~")
    AddCommit("Point elsewhere").run(s)
    # As an editor saves it, with a line ending, which a link has no use for.
    assert Edit(1, "link", b"README\n").run(s).message == "link is unchanged in commit 1."
    Edit(1, "link", b"elsewhere\n").run(s)
    (sha,) = s.build()
    assert repo.git("ls-tree", sha, "link").startswith("120000 blob")
    assert repo.git("cat-file", "blob", f"{sha}:link") == "elsewhere"


def test_a_submodule_cant_be_edited(repo: Repo) -> None:
    sha = repo.git("rev-parse", "HEAD")
    # One the range adds, and one it doesn't change (each unpopulated, an
    # empty directory).
    for name in ("old_sub", "sub"):
        (repo.path / name).mkdir()
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{sha},old_sub")
    repo.git("commit", "--quiet", "-m", "add old_sub")
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
    repo.git("commit", "--quiet", "-m", "add sub")
    s = repo.session("HEAD~")
    AddCommit("one").run(s)
    for name in ("old_sub", "sub"):
        with pytest.raises(RecommitError, match=f"{name} is a submodule, so it can't be edited"):
            Edit(1, name, b"x").run(s)


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_file_that_becomes_a_symlink_is_edited_as_what_it_is_in_the_commit(repo: Repo) -> None:
    repo.commit("base", link="a file\n")
    (repo.path / "link").unlink()
    (repo.path / "link").symlink_to("README")
    repo.commit("change", f="f\n")
    s = repo.session("HEAD~")
    AddCommit("Before").run(s)
    AddCommit("After").run(s)
    Assign((Target("link"),), 2).run(s)
    Edit(1, "link", b"still a file\n").run(s)
    Edit(2, "link", b"elsewhere\n").run(s)
    first, second = s.build()
    assert repo.show(first, "link") == "still a file\n"
    assert repo.git("ls-tree", second, "link").startswith("120000") and repo.show(second, "link") == "elsewhere"


def test_a_binary_conflict_can_be_resolved_by_keeping_the_edit(repo: Repo) -> None:
    s = binary_session(repo)
    AddCommit("Stub data").run(s)
    AddCommit("Change data").run(s)
    Assign((Target("f"),), 1).run(s)
    Assign((Target("data"),), 2).run(s)
    Edit(1, "data", b"\0stub").run(s)
    assert s.conflicts() != []
    # What it opens with (the edit carried in), kept.
    assert s.edit_text(s.plan.commits[1].key, s.files[0]) == b"\0stub"
    Edit(2, "data", b"\0stub").run(s)
    assert s.conflicts() == []
    _, second = s.build()
    assert repo.git("cat-file", "blob", f"{second}:data") == "\0stub"


# An edit with a NUL, and one without: it's merged as binary either way,
# since the other sides have a NUL.
@pytest.mark.parametrize("stub", [b"a\0\nstub\n", b"stub\n"], ids=["nul", "no-nul"])
def test_a_file_with_nuls_diffed_as_text_conflicts_as_a_binary_file(repo: Repo, stub: bytes) -> None:
    # git won't merge it, so a change on both sides conflicts, and it opens
    # with no conflict markers: as the edit carried in, which saving keeps.
    repo.commit("base", data=b"a\0\nb\n", **{".gitattributes": "data diff\n"})
    repo.commit("change", data=b"a\0\nb\nc\n")
    s = repo.session("HEAD~")
    AddCommit("Stub data").run(s)
    AddCommit("Change data").run(s)
    Assign((Target("data"),), 2).run(s)
    Edit(1, "data", stub).run(s)
    assert [(s.plan.index(c.commit.key), c.path) for c in s.conflicts()] == [(1, "data")]
    assert s.edit_text(s.plan.commits[1].key, s.files[0]) == stub
    Edit(2, "data", stub).run(s)
    assert s.conflicts() == []


def test_a_conflict_with_markers_isnt_resolved_by_saving_it_unchanged(repo: Repo) -> None:
    # A NUL past git's first 8000 bytes: merged line by line, with markers,
    # though the lines both sides delete bring the NUL into the first 8000.
    lines = [f"{i:099d}\n" for i in range(90)]

    def data(first: str, gone: range) -> bytes:
        kept = [first] + [line for i, line in enumerate(lines) if i and i not in gone]
        return "".join(kept).encode() + b"z\0\n"

    repo.commit("base", data="".join(lines).encode() + b"z\0\n")
    repo.commit("change", data=data("theirs\n", range(10, 18)))
    s = repo.session("HEAD~")
    AddCommit("Edit data").run(s)
    AddCommit("Change data").run(s)
    Assign((Target("data"),), 2).run(s)
    Edit(1, "data", data("ours\n", range(30, 38))).run(s)
    opened = s.edit_text(s.plan.commits[1].key, s.files[0])
    assert b"<<<<<<< edit" in opened and b"\0" in opened[:8000]
    assert Edit(2, "data", opened).run(s).message == "data is unchanged in commit 2."
    assert [(s.plan.index(c.commit.key), c.path) for c in s.conflicts()] == [(1, "data")]


def nul_session(repo: Repo) -> Session:
    """data, with a NUL, diffed as text: c added."""
    repo.commit("base", data=b"a\0\nb\n", **{".gitattributes": "data diff\n"})
    repo.commit("change", data=b"a\0\nb\nc\n")
    return repo.session("HEAD~")


def test_an_edit_to_a_file_with_nuls_conflicts_with_more_changes_in_its_commit(repo: Repo) -> None:
    s = nul_session(repo)
    AddCommit("Edit data").run(s)
    Edit(1, "data", b"a\0\nstub\n").run(s)
    Assign((Target("data"),), 1).run(s)
    assert [(s.plan.index(c.commit.key), c.path) for c in s.conflicts()] == [(0, "data")]


def test_deleting_a_file_with_nuls_conflicts_with_a_later_change_as_a_deletion(repo: Repo) -> None:
    s = nul_session(repo)
    AddCommit("Delete data").run(s)
    AddCommit("Change data").run(s)
    Assign((Target("data"),), 2).run(s)
    Edit(1, "data", None).run(s)
    assert [(s.plan.index(c.commit.key), c.path) for c in s.conflicts()] == [(1, "data")]
    assert s.states(s.files[0])[1].version is None
    Edit(2, "data", None).run(s)
    assert s.conflicts() == []
    _, second = s.build()
    assert repo.show(second, "data") is None


@pytest.mark.parametrize("third_takes_the_nul", [True, False], ids=["merged-as-binary", "taking-nothing"])
def test_conflict_markers_are_carried_into_later_commits(repo: Repo, third_takes_the_nul: bool) -> None:
    # A conflict with markers in the second commit; the third keeps them,
    # even merging as binary (keeping them, as the edit carried in), so
    # saving them there is no resolution either.
    repo.commit("base", data=LINES, **{".gitattributes": "data diff\n"})
    repo.commit("change", data=LINES.replace("1\n", "one\n", 1).encode() + b"z\0\n")
    s = repo.session("HEAD~")
    for message in ("Edit data", "Change data", "Add a NUL"):
        AddCommit(message).run(s)
    Assign((Target("data", hunk=1),), 2).run(s)
    if third_takes_the_nul:
        Assign((Target("data", hunk=2),), 3).run(s)
    Edit(1, "data", LINES.replace("1\n", "uno\n", 1).encode()).run(s)
    third = s.plan.commits[2].key
    opened = s.edit_text(third, s.files[0])
    assert b"<<<<<<< edit" in opened
    assert Edit(3, "data", opened).run(s).message == "data is unchanged in commit 3."


def test_deleting_a_binary_file_conflicts_with_a_later_change_to_it(repo: Repo) -> None:
    s = binary_session(repo)
    AddCommit("Delete data").run(s)
    AddCommit("Change data").run(s)
    Assign((Target("f"),), 1).run(s)
    Assign((Target("data"),), 2).run(s)
    Edit(1, "data", None).run(s)
    assert [(s.plan.index(c.commit.key), c.path) for c in s.conflicts()] == [(1, "data")]
    # Deleting it there too resolves it.
    Edit(2, "data", None).run(s)
    assert s.conflicts() == []


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_editing_a_symlink_the_range_doesnt_change(repo: Repo) -> None:
    (repo.path / "link").symlink_to("README")
    repo.commit("base")
    repo.commit("change", f="f\n")
    s = repo.session("HEAD~")
    AddCommit("Point elsewhere").run(s)
    Edit(1, "link", b"f\n").run(s)
    (sha,) = s.build()
    assert repo.git("ls-tree", sha, "link").startswith("120000") and repo.show(sha, "link") == "f"
