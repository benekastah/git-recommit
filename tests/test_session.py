import pytest

from git_recommit.errors import RecommitError
from git_recommit.session import Session, resolve_range
from tests.helpers import Repo

FEATURE = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"


def feature_repo(repo: Repo) -> list[str]:
    """Three commits on top of init that build a.py and b.py, the history of
    development rather than reviewable pieces."""
    return [
        repo.commit("wip", **{"a.py": "def a():\n    return 0\n"}),
        repo.commit("more wip", **{"a.py": FEATURE, "notes.txt": "todo\n"}),
        repo.commit("done", **{"b.py": "import a\n", "notes.txt": None}),
    ]


def files_ids(session: Session, path: str) -> list[int]:
    return next(d for d in session.files if d.path == path).ids


def test_resolve_range(repo: Repo) -> None:
    init = repo.git("rev-parse", "HEAD")
    first, _, head = feature_repo(repo)
    git = repo.client
    assert resolve_range(git, init) == (init, head)
    assert resolve_range(git, "HEAD~3") == (init, head)
    assert resolve_range(git, "HEAD~3..HEAD") == (init, head)
    # Either way round, with two dots or three.
    assert resolve_range(git, "HEAD...HEAD~3") == (init, head)
    assert resolve_range(git, "HEAD..HEAD~2") == (first, head)
    with pytest.raises(RecommitError, match="isn't a commit"):
        resolve_range(git, "nope")
    # No commits: only the uncommitted changes (see test_only_uncommitted_changes).
    assert resolve_range(git, "HEAD") == (head, head)
    with pytest.raises(RecommitError, match="doesn't end at HEAD"):
        resolve_range(git, "HEAD~3..HEAD~1")


def test_resolve_range_refuses_merges(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "side")
    repo.commit("side", s="s\n")
    repo.git("switch", "--quiet", "main")
    repo.commit("main", m="m\n")
    repo.git("merge", "--quiet", "--no-edit", "side")
    with pytest.raises(RecommitError, match="merge commits"):
        resolve_range(repo.client, "HEAD~2")


def test_uncommitted_changes_are_changes_too(repo: Repo) -> None:
    feature_repo(repo)
    repo.write("a.py", FEATURE + "\n\ndef c():\n    return 3\n")
    repo.write("staged.txt", "new\n")
    repo.git("add", "staged.txt")
    (repo.path / "b.py").unlink()
    repo.write("untracked.txt", "ignored\n")
    session = repo.session("HEAD~3")
    assert session.uncommitted
    # b.py is gone, so it isn't a change; untracked files aren't included.
    assert [d.path for d in session.files] == ["a.py", "staged.txt"]
    plan = session.plan
    plan.assign([i for d in session.files for i in d.ids], plan.add("everything").key)
    session.finish(session.build())

    assert repo.show("HEAD", "a.py") == FEATURE + "\n\ndef c():\n    return 3\n"
    assert repo.show("HEAD", "staged.txt") == "new\n" and repo.show("HEAD", "b.py") is None
    assert repo.git("status", "--porcelain") == "?? untracked.txt"


def test_unassigned_uncommitted_changes_stay_uncommitted(repo: Repo) -> None:
    feature_repo(repo)
    repo.write("a.py", "changed\n")
    session = repo.session("HEAD~3")
    plan = session.plan
    plan.assign(files_ids(session, "b.py"), plan.add("Add b").key)
    session.finish(session.build())

    assert repo.git("log", "--format=%s", "-1") == "Add b"
    assert (repo.path / "a.py").read_text() == "changed\n"
    assert repo.git("status", "--porcelain") == "?? a.py"


def test_only_uncommitted_changes(repo: Repo) -> None:
    with pytest.raises(RecommitError, match="no uncommitted changes"):
        repo.session("HEAD")
    repo.write("README", "changed\n")
    session = repo.session("HEAD")
    assert session.old_commits == [] and [d.path for d in session.files] == ["README"]
    plan = session.plan
    plan.assign(session.files[0].ids, plan.add("Change the README").key)
    session.finish(session.build())
    assert repo.git("log", "--format=%s", "-2").splitlines() == ["Change the README", "init"]
    assert repo.git("status", "--porcelain") == ""


def test_reload_keeps_assignments(repo: Repo) -> None:
    repo.commit("base", f="a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
    repo.commit("change", f="A\nb\nc\nd\ne\nf\ng\nh\ni\nJ\n")
    session = repo.session("HEAD~")
    assert not session.reload()
    remove_a, add_a, remove_j, _add_j = session.files[0].ids
    plan = session.plan
    first, second = plan.add("first"), plan.add("second")
    plan.assign([remove_a, add_a], first.key)
    plan.assign([remove_j], second.key)

    # A new change between them, which shifts the ids.
    repo.write("f", "A\nb\nc\nd\nE\nf\ng\nh\ni\nJ\n")
    assert session.reload()
    assert session.uncommitted
    texts = {line.id: line.kind + line.text.decode() for h in session.files[0].hunks for line in h.lines}
    assigned = {texts[i]: plan.index(key) for i, key in plan.assignment.items()}
    assert assigned == {"-a\n": 0, "+A\n": 0, "-j\n": 1}
    assert sorted(texts[i] for i in session.unassigned()) == ["+E\n", "+J\n", "-e\n"]

    # Committing them (while recommit is in the background) changes HEAD.
    repo.git("commit", "--quiet", "-am", "more")
    assert session.reload()
    assert not session.uncommitted and session.old_commits[0].endswith(" more")
    assert len(plan.assignment) == 3
    plan.assign(session.unassigned(), second.key)
    session.finish(session.build())
    assert repo.git("log", "--format=%s", "-2").splitlines() == ["second", "first"]
    assert repo.git("status", "--porcelain") == ""


def test_reload_refuses_when_nothing_is_left(repo: Repo) -> None:
    repo.commit("change", README="changed\n")
    session = repo.session("HEAD~")
    repo.write("README", "readme\n")
    with pytest.raises(RecommitError, match="no changes any more"):
        session.reload()


def test_rebuilds_commits_from_assigned_changes(repo: Repo) -> None:
    feature_repo(repo)
    old_tip_tree = repo.git("rev-parse", "HEAD^{tree}")
    session = repo.session("HEAD~3")
    # notes.txt was added and removed again, so it isn't a change at all.
    assert [d.path for d in session.files] == ["a.py", "b.py"]
    plan = session.plan
    first = plan.add("Add a")
    second = plan.add("Add b")
    plan.assign(files_ids(session, "a.py"), first.key)
    plan.assign(files_ids(session, "b.py"), second.key)
    assert session.unassigned() == [] and session.left_uncommitted() == []
    shas = session.build()
    session.finish(shas)

    assert repo.git("log", "--format=%s", "HEAD~2..HEAD").splitlines() == ["Add b", "Add a"]
    assert repo.show("HEAD~", "a.py") == FEATURE and repo.show("HEAD~", "b.py") is None
    assert repo.git("rev-parse", "HEAD^{tree}") == old_tip_tree
    assert repo.git("status", "--porcelain") == ""


def test_splits_a_file_between_commits(repo: Repo) -> None:
    repo.commit("base", f="1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n")
    repo.commit("change", f="one\n2\n3\n4\n5\n6\n7\n8\n9\nten\n")
    session = repo.session("HEAD~")
    remove_1, add_one, remove_10, add_ten = files_ids(session, "f")
    plan = session.plan
    plan.assign([remove_10, add_ten], plan.add("ten").key)
    plan.assign([remove_1, add_one], plan.add("one").key)
    first, second = session.build()
    assert repo.show(first, "f") == "1\n2\n3\n4\n5\n6\n7\n8\n9\nten\n"
    assert repo.show(second, "f") == "one\n2\n3\n4\n5\n6\n7\n8\n9\nten\n"


def test_unassigned_changes_are_left_uncommitted(repo: Repo) -> None:
    feature_repo(repo)
    session = repo.session("HEAD~3")
    plan = session.plan
    plan.assign(files_ids(session, "a.py"), plan.add("Add a").key)
    assert session.unassigned() == files_ids(session, "b.py")
    assert session.left_uncommitted() == ["b.py"]
    session.finish(session.build())

    assert repo.git("log", "--format=%s", "-1") == "Add a"
    assert repo.show("HEAD", "b.py") is None
    # b.py is in the working tree, untracked; the index matches HEAD.
    assert repo.git("status", "--porcelain") == "?? b.py"


def test_mode_binary_and_deletions(repo: Repo) -> None:
    repo.commit("base", data=b"\0old", gone="1\n2\n", script="echo\n")
    repo.write("data", b"\0new")
    (repo.path / "script").chmod(0o755)
    repo.commit("change", gone=None)
    session = repo.session("HEAD~")
    plan = session.plan
    gone_1, gone_2 = files_ids(session, "gone")
    plan.assign([gone_1, *files_ids(session, "data")], plan.add("one").key)
    plan.assign([gone_2, *files_ids(session, "script")], plan.add("two").key)
    first, second = session.build()
    assert repo.show(first, "gone") == "2\n" and repo.show(second, "gone") is None
    assert repo.git("cat-file", "blob", f"{first}:data") == "\0new"
    assert "100644" in repo.git("ls-tree", first, "script") and "100755" in repo.git("ls-tree", second, "script")


def test_an_edit_is_carried_into_later_commits(repo: Repo) -> None:
    repo.commit("base", f="a\nb\nc\nd\ne\nf\ng\nh\n")
    repo.commit("change", f="A\nb\nc\nd\ne\nf\ng\nH\n")
    session = repo.session("HEAD~")
    diff = session.files[0]
    remove_a, add_a, remove_h, add_h = diff.ids
    plan = session.plan
    first, second = plan.add("first"), plan.add("second")
    plan.assign([remove_a, add_a], first.key)
    plan.assign([remove_h, add_h], second.key)
    session.set_edit(first.key, diff, b"A\nb\nc\nd\nEDITED\nf\ng\nh\n")
    assert [s.carried for s in session.states(diff)] == [True, True]
    one, two = session.build()
    assert repo.show(one, "f") == "A\nb\nc\nd\nEDITED\nf\ng\nh\n"
    assert repo.show(two, "f") == "A\nb\nc\nd\nEDITED\nf\ng\nH\n"
    # The edit survives into the last commit, so it isn't the end state.
    assert session.left_uncommitted() == ["f"]


def test_an_edit_is_kept_when_its_commit_takes_more_changes(repo: Repo) -> None:
    repo.commit("base", f="a\nb\nc\nd\ne\nf\ng\nh\n")
    repo.commit("change", f="A\nb\nc\nd\ne\nf\ng\nH\n")
    session = repo.session("HEAD~")
    diff = session.files[0]
    remove_a, add_a, remove_h, add_h = diff.ids
    commit = session.plan.add("only")
    session.plan.assign([remove_a, add_a], commit.key)
    session.set_edit(commit.key, diff, b"A\nb\nc\nd\nEDITED\nf\ng\nh\n")
    session.plan.assign([remove_h, add_h], commit.key)
    assert session.edit_text(commit.key, diff) == b"A\nb\nc\nd\nEDITED\nf\ng\nH\n"


def test_an_overlapping_change_conflicts_until_resolved(repo: Repo) -> None:
    repo.commit("base", f="def a():\n    return 1\n")
    repo.commit("change", f="def a():\n    return compute()\n")
    session = repo.session("HEAD~")
    diff = session.files[0]
    plan = session.plan
    stub, real = plan.add("stub"), plan.add("real")
    plan.assign(diff.ids, real.key)
    session.set_edit(stub.key, diff, b"def a():\n    return 2\n")
    conflicts = session.conflicts()
    assert [(c.commit.key, c.path) for c in conflicts] == [(real.key, "f")]
    assert b"<<<<<<< edit" in session.edit_text(real.key, diff)
    with pytest.raises(RecommitError, match="conflicts"):
        session.build()

    session.set_edit(real.key, diff, b"def a():\n    return compute()\n")
    assert session.conflicts() == []
    _, two = session.build()
    assert repo.show(two, "f") == "def a():\n    return compute()\n"
    assert session.left_uncommitted() == []


def diverge(repo: Repo) -> None:
    """main and side both change f, so merging or cherry-picking side
    conflicts; clean changes g, which merges cleanly."""
    repo.commit("base", f="1\n", g="g\n")
    repo.git("switch", "--quiet", "-c", "side")
    repo.commit("side", f="side\n")
    repo.git("switch", "--quiet", "-c", "clean", "main")
    repo.commit("clean", g="clean\n")
    repo.git("switch", "--quiet", "main")
    repo.commit("main", f="main\n")


@pytest.mark.parametrize(
    ("start", "error"),
    [
        (["merge", "side"], "a merge is in progress"),
        (["merge", "--no-commit", "--no-ff", "clean"], "a merge is in progress"),
        (["cherry-pick", "side"], "a cherry-pick is in progress"),
        (["revert", "--no-commit", "HEAD"], "a revert is in progress"),
    ],
)
def test_refuses_to_start_in_the_middle_of_another_operation(repo: Repo, start: list[str], error: str) -> None:
    diverge(repo)
    repo.client.ok(*start)
    with pytest.raises(RecommitError, match=error):
        repo.session("HEAD~2")


def test_refuses_to_start_with_unresolved_conflicts(repo: Repo) -> None:
    # A conflicted `git stash pop` leaves conflicts, but no MERGE_HEAD.
    diverge(repo)
    repo.write("f", "stashed\n")
    repo.git("stash", "--quiet")
    repo.write("f", "different\n")
    repo.git("commit", "--quiet", "-am", "different")
    repo.client.ok("stash", "pop")
    assert repo.git("ls-files", "--unmerged")
    with pytest.raises(RecommitError, match="unresolved conflicts"):
        repo.session("HEAD~3")


def test_reloading_and_writing_refuse_once_another_operation_starts(repo: Repo) -> None:
    diverge(repo)
    session = repo.session("HEAD~2")
    session.restore_old_commits()
    shas = session.build()
    head = repo.git("rev-parse", "HEAD")
    repo.client.ok("merge", "side")
    with pytest.raises(RecommitError, match="a merge is in progress"):
        session.reload()
    with pytest.raises(RecommitError, match="a merge is in progress"):
        session.finish(shas)
    # Nothing changed: the merge can still be aborted.
    assert repo.git("rev-parse", "HEAD") == head
    repo.git("merge", "--abort")


def test_writing_after_head_moved_says_where_the_new_commits_are(repo: Repo) -> None:
    feature_repo(repo)
    session = repo.session("HEAD~3")
    session.restore_old_commits()
    shas = session.build()
    moved = repo.commit("meanwhile", c="c\n")
    with pytest.raises(RecommitError, match=f"HEAD moved.*{shas[-1]}"):
        session.finish(shas)
    assert repo.git("rev-parse", "HEAD") == moved
