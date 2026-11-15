"""Starting from the commits being rebuilt (Session.restore_old_commits)."""

import pytest

from git_recommit import oldcommits
from git_recommit.errors import RecommitError
from git_recommit.session import Session
from tests.helpers import Repo

LINES = "".join(f"line {i}\n" for i in range(1, 21))


def history(repo: Repo) -> list[str]:
    """Three commits that overlap: the second rewrites a line the first added
    ("five", to "FIVE"), and removes a line that was there before."""
    repo.commit("base", f=LINES)
    return [
        repo.commit("first", f=LINES.replace("line 5\n", "line 5\nfive\n"), g="g\n"),
        repo.commit("second", f=LINES.replace("line 5\n", "line 5\nFIVE\n").replace("line 15\n", ""), data=b"\0bin"),
        repo.commit("third", h="h\n", g="g\ng2\n"),
    ]


def trees(repo: Repo, shas: list[str]) -> list[str]:
    return [repo.git("rev-parse", f"{sha}^{{tree}}") for sha in shas]


def restored(repo: Repo, spec: str) -> Session:
    session = repo.session(spec)
    session.restore_old_commits()
    return session


def test_starts_with_the_old_commits_and_their_messages(repo: Repo) -> None:
    history(repo)
    session = restored(repo, "HEAD~3")
    assert [c.message for c in session.plan.commits] == ["first", "second", "third"]
    assert session.unassigned() == []


def test_rebuilding_them_unchanged_makes_the_same_trees(repo: Repo) -> None:
    old = history(repo)
    session = restored(repo, "HEAD~3")
    assert trees(repo, session.build()) == trees(repo, old)


def test_overlapping_changes_are_reproduced_with_edits(repo: Repo) -> None:
    history(repo)
    session = restored(repo, "HEAD~3")
    first, second, third = session.plan.commits
    # "five" is gone by the end, so only an edit can put it in the first
    # commit; the second rewrites it, so it needs one too, to resolve carrying
    # the first's edit into it.
    assert (
        set(first.edits) == {"f"} and first.edits["f"].content == LINES.replace("line 5\n", "line 5\nfive\n").encode()
    )
    assert set(second.edits) == {"f"}
    assert third.edits == {}


def test_building_leaves_the_real_index_alone(repo: Repo) -> None:
    history(repo)
    repo.write("h", "h\nchanged\n")
    repo.git("add", "h")
    index = repo.git("write-tree")
    restored(repo, "HEAD~3").build()
    assert repo.git("write-tree") == index


def test_keeps_the_old_commits_authors(repo: Repo) -> None:
    history(repo)
    repo.git(
        "commit",
        "--quiet",
        "--amend",
        "--no-edit",
        "--author=Someone Else <else@example.com>",
        "--date=2001-02-03T04:05:06+07:00",
    )
    session = restored(repo, "HEAD~3")
    session.finish(session.build())
    log = repo.git("log", "--format=%an <%ae> %ad", "--date=iso-strict", "-3").splitlines()
    assert log[0] == "Someone Else <else@example.com> 2001-02-03T04:05:06+07:00"
    assert log[1].startswith("Test <test@example.com> ")


def test_new_commits_are_the_users(repo: Repo) -> None:
    history(repo)
    repo.git("commit", "--quiet", "--amend", "--no-edit", "--author=Someone Else <else@example.com>")
    session = restored(repo, "HEAD~3")
    session.plan.add("mine")
    session.finish(session.build())
    assert repo.git("log", "--format=%an", "-1") == "Test"


def test_uncommitted_changes_start_unassigned(repo: Repo) -> None:
    history(repo)
    repo.write("h", "h\nchanged\n")
    session = restored(repo, "HEAD~3")
    unassigned = session.unassigned()
    texts = {line.id: line.kind + line.text.decode() for d in session.files for h in d.hunks for line in h.lines}
    assert sorted(texts[i] for i in unassigned) == ["+changed\n"]


def test_clearing_starts_from_scratch(repo: Repo) -> None:
    history(repo)
    session = restored(repo, "HEAD~3")
    session.clear()
    assert session.plan.commits == [] and len(session.unassigned()) == len([i for d in session.files for i in d.ids])


def test_changes_blame_cant_attribute_go_to_the_last_commit_to_change_the_file(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git blame and the diff can match lines differently (e.g. a blank line
    # that moves), leaving blame nothing to say about a change.
    old = history(repo)
    monkeypatch.setattr(oldcommits, "_blame", lambda session, path, reverse: {})
    session = restored(repo, "HEAD~3")
    assert session.unassigned() == []
    _, second, third = session.plan.commits
    by_file = {d.path: {session.plan.assignment[i] for i in d.ids} for d in session.files}
    assert by_file["f"] == {second.key} and by_file["g"] == {third.key} and by_file["h"] == {third.key}
    assert trees(repo, session.build()) == trees(repo, old)


def test_paths_that_look_like_patterns(repo: Repo) -> None:
    # As a pathspec, a[1].txt would match a1.txt, and :x is magic.
    repo.commit("base", **{"a1.txt": "1\n", "a[1].txt": "1\n", ":x": "1\n"})
    old = [
        repo.commit("first", **{"a1.txt": "1\nfirst\n"}),
        repo.commit("second", **{"a[1].txt": "1\nsecond\n", ":x": "1\nsecond\n"}),
    ]
    session = restored(repo, "HEAD~2")
    by_file = {d.path: {session.plan.index(session.plan.assignment[i]) for i in d.ids} for d in session.files}
    assert by_file == {"a1.txt": {0}, "a[1].txt": {1}, ":x": {1}}
    assert trees(repo, session.build()) == trees(repo, old)
    session.plan.assign(next(d for d in session.files if d.path == "a[1].txt").ids, None)
    session.finish(session.build())
    assert repo.git("status", "--porcelain") == "M a[1].txt"


def by_someone_else(repo: Repo) -> Session:
    """The first of history's commits is by someone else."""
    old = history(repo)
    repo.git("reset", "--quiet", "--hard", old[0])
    repo.git("commit", "--quiet", "--amend", "--no-edit", "--author=Someone Else <else@example.com>")
    repo.git("cherry-pick", "--quiet", old[1], old[2])
    return restored(repo, "HEAD~3")


def test_warns_about_commits_taking_someone_elses_changes(repo: Repo) -> None:
    session = by_someone_else(repo)
    assert session.authorship_warnings() == []
    plan = session.plan
    mine = plan.add("mine")
    plan.assign(next(d for d in session.files if d.path == "g").ids, mine.key)
    (warning,) = session.authorship_warnings()
    assert warning.startswith("commit 4 (mine) will be authored by Test <test@example.com>")
    assert "Someone Else <else@example.com>" in warning


def test_choosing_a_commits_author(repo: Repo) -> None:
    from git_recommit.commands import AddCommit, SetAuthor

    session = by_someone_else(repo)
    AddCommit("mine").run(session)
    plan = session.plan
    theirs = [i for i, author in session.origin.items() if author.name == "Someone Else"]
    plan.assign(theirs, plan.commits[3].key)
    assert SetAuthor(4, "Someone Else <else@example.com>").run(session).message == (
        "Commit 4 is now by Someone Else <else@example.com>."
    )
    assert session.authorship_warnings() == []
    shas = session.build()
    assert repo.git("log", "-1", "--format=%an <%ae>", shas[3]) == "Someone Else <else@example.com>"
    # Back to whoever runs git recommit.
    SetAuthor(4, None).run(session)
    assert repo.git("log", "-1", "--format=%an", session.build()[3]) == "Test"
    with pytest.raises(RecommitError, match="isn't Name <email>"):
        SetAuthor(4, "nobody").run(session)


def test_rebuilding_from_the_root(repo: Repo) -> None:
    old = [repo.git("rev-list", "--max-parents=0", "HEAD"), *history(repo)]
    old.insert(1, repo.git("rev-parse", "HEAD~3"))  # base, between init and history
    session = Session.load(repo.client, None)
    session.restore_old_commits()
    assert [c.message for c in session.plan.commits] == ["init", "base", "first", "second", "third"]
    shas = session.build()
    assert trees(repo, shas) == trees(repo, old)
    # The first has no parent, as the root commit it replaces.
    assert repo.git("rev-list", "--parents", "-1", shas[0]) == shas[0]
    session.finish(shas)
    assert repo.git("rev-list", "--count", "HEAD") == "5"
    assert repo.git("status", "--porcelain") == ""
