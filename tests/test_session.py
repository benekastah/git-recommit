import os
import pathlib
import subprocess

import pytest

from git_recommit.errors import RecommitError
from git_recommit.revisions import resolve_range
from git_recommit.session import Session
from git_recommit.snapshot import Version
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
    assert resolve_range(git, "HEAD~3..") == (init, head)
    # As in git: a...b is the commits on either side but not both, so either
    # way round...
    assert resolve_range(git, "HEAD...HEAD~3") == (init, head)
    assert resolve_range(git, "HEAD~2...HEAD") == (first, head)
    # ...but a..b is those in b and not a, so none, the wrong way round.
    with pytest.raises(RecommitError, match="HEAD..HEAD~2 has no commits"):
        resolve_range(git, "HEAD..HEAD~2")
    with pytest.raises(RecommitError, match="isn't a commit"):
        resolve_range(git, "nope")
    # No commits: only the uncommitted changes (see test_only_uncommitted_changes).
    assert resolve_range(git, "HEAD") == (head, head)
    # A range can end below HEAD: the commits after it are replayed.
    assert resolve_range(git, "HEAD~3..HEAD~1") == (init, repo.git("rev-parse", "HEAD~"))


def test_resolve_range_of_another_branch(repo: Repo) -> None:
    """a..b, where b is HEAD and a is on another branch, is the commits since
    they forked, as in git (e.g. main..HEAD, after main moved on)."""
    fork = repo.git("rev-parse", "HEAD")
    repo.git("branch", "other")
    repo.commit("mine", m="m\n")
    repo.git("switch", "--quiet", "other")
    repo.commit("theirs", t="t\n")
    repo.git("switch", "--quiet", "-")
    head = repo.git("rev-parse", "HEAD")
    assert resolve_range(repo.client, "other..HEAD") == (fork, head)
    assert resolve_range(repo.client, "other..") == (fork, head)
    with pytest.raises(RecommitError, match="on two branches"):
        resolve_range(repo.client, "other...HEAD")


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
    # Added with --intent-to-add (see test_unassigned_changes_are_left_uncommitted).
    assert repo.git("status", "--porcelain") == "A a.py"


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
    # b.py is still a file git knows about (added with --intent-to-add), not
    # an untracked one, so it's an uncommitted change, and the next recommit
    # sees it.
    assert repo.git("status", "--porcelain") == "A b.py"
    assert [d.path for d in repo.session("HEAD").files] == ["b.py"]


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


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_building_a_change_of_kind(repo: Repo) -> None:
    repo.commit("base", link="a file\n")
    (repo.path / "link").unlink()
    (repo.path / "link").symlink_to("target")
    repo.commit("change")
    session = repo.session("HEAD~")
    session.restore_old_commits()
    (sha,) = session.build()
    assert repo.git("ls-tree", sha, "link").startswith("120000 blob")


def test_building_a_file_whose_name_isnt_utf8(repo: Repo) -> None:
    # Made with plumbing, since some filesystems (e.g. macOS's) refuse such
    # names; skip-worktree keeps git from seeing it as deleted from the
    # working tree.
    name = b"caf\xe9.txt"
    blob = repo.client.hash_blob(b"x\n")
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", b"100644," + blob.encode() + b"," + name],
        cwd=repo.path,
        check=True,
    )
    repo.git("commit", "--quiet", "-m", "add")
    subprocess.run(["git", "update-index", "--skip-worktree", name], cwd=repo.path, check=True)
    session = repo.session("HEAD~")
    session.restore_old_commits()
    (sha,) = session.build()
    listed = subprocess.run(
        ["git", "ls-tree", "-z", "--name-only", sha], cwd=repo.path, capture_output=True, check=True
    )
    assert name in listed.stdout.split(b"\0")


def test_reloading_drops_edits_of_files_no_longer_changed(repo: Repo) -> None:
    from git_recommit.board import Board
    from git_recommit.commands import Edit

    repo.commit("wip", a="real\n", b="b\n")
    session = repo.session("HEAD~")
    session.restore_old_commits()
    Edit(1, "a", b"stub\n").run(session)
    board = Board(session)
    # a is added by the range; deleting it means the range doesn't change it.
    (repo.path / "a").unlink()
    board.reload()
    assert [d.path for d in session.files] == ["b"]
    assert session.plan.commits[0].edits == {}
    assert "dropped commit 1's edit of a" in board.message


def script(path: pathlib.Path, body: str) -> str:
    """An executable shell script at path, returning its path."""
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as gpg")
def test_signs_commits_when_git_is_set_to(repo: Repo, tmp_path: pathlib.Path) -> None:
    # A stand-in for gpg, which says it signed, as git checks it does.
    gpg = script(
        tmp_path / "gpg",
        "cat > /dev/null\n"
        "printf '\\n[GNUPG:] SIG_CREATED D 1 8 00 0 0\\n' >&2\n"
        "printf -- '-----BEGIN PGP SIGNATURE-----\\nfake\\n-----END PGP SIGNATURE-----\\n'\n",
    )
    repo.git("config", "gpg.program", gpg)
    repo.git("config", "user.signingKey", "test@example.com")
    repo.git("config", "commit.gpgSign", "true")
    feature_repo(repo)
    session = repo.session("HEAD~3")
    session.restore_old_commits()
    for sha in session.build():
        assert "gpgsig -----BEGIN PGP SIGNATURE-----" in repo.git("cat-file", "commit", sha)


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as a hook")
def test_runs_the_commit_msg_hook_on_new_and_reworded_messages(repo: Repo) -> None:
    feature_repo(repo)
    hooks = repo.path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    script(hooks / "commit-msg", 'printf "\\nChange-Id: I123\\n" >> "$1"\n')
    session = repo.session("HEAD~3")
    session.restore_old_commits()
    plan = session.plan
    plan.commits[1].message = "more wip, reworded"
    plan.add("mine")
    messages = [repo.git("log", "-1", "--format=%B", sha) for sha in session.build()]
    assert messages == ["wip", "more wip, reworded\n\nChange-Id: I123", "done", "mine\n\nChange-Id: I123"]


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as a hook")
def test_a_commit_msg_hook_can_refuse_a_message(repo: Repo) -> None:
    feature_repo(repo)
    hooks = repo.path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    script(hooks / "commit-msg", "echo 'no thanks' >&2\nexit 1\n")
    session = repo.session("HEAD~3")
    session.plan.add("mine")
    with pytest.raises(RecommitError, match="(?s)commit-msg hook refused commit 1.*no thanks"):
        session.build()


def test_writing_refuses_while_the_index_is_locked(repo: Repo) -> None:
    feature_repo(repo)
    head = repo.git("rev-parse", "HEAD")
    session = repo.session("HEAD~3")
    session.restore_old_commits()
    shas = session.build()
    (repo.path / ".git" / "index.lock").touch()
    with pytest.raises(RecommitError, match="index.lock"):
        session.finish(shas)
    # Not half done: HEAD hasn't moved without the index.
    assert repo.git("rev-parse", "HEAD") == head


def test_loading_doesnt_write_the_index(repo: Repo) -> None:
    feature_repo(repo)
    # Its stat information is out of date, which git status would refresh.
    os.utime(repo.path / "a.py", (1, 1))
    index = (repo.path / ".git" / "index").read_bytes()
    repo.session("HEAD~3")
    assert (repo.path / ".git" / "index").read_bytes() == index


def test_old_messages_are_kept_exactly(repo: Repo) -> None:
    repo.commit("base", a="1\n")
    repo.write("a", "2\n")
    message = "  An indented subject\n\n    and a body\n"
    repo.git("commit", "--quiet", "--all", "--cleanup=verbatim", "-m", message)
    session = repo.session("HEAD~")
    session.restore_old_commits()
    (sha,) = session.build()
    assert repo.git("log", "-1", "--format=%B", sha) == repo.git("log", "-1", "--format=%B", "HEAD")
    raw = subprocess.run(["git", "cat-file", "commit", sha], cwd=repo.path, capture_output=True, check=True).stdout
    assert raw.endswith(b"\n\n" + message.encode())


def test_unassigned_new_files_that_look_like_patterns(repo: Repo) -> None:
    # As a pathspec, b[1].txt would also match b1.txt.
    repo.commit("wip", **{"b1.txt": "1\n", "b[1].txt": "1\n"})
    session = repo.session("HEAD~")
    plan = session.plan
    plan.assign(files_ids(session, "b1.txt"), plan.add("b1").key)
    session.finish(session.build())
    assert sorted(repo.git("status", "--porcelain").splitlines()) == ["A b[1].txt"]


def test_including_untracked_files(repo: Repo) -> None:
    feature_repo(repo)
    repo.write(".gitignore", "*.log\n")
    repo.git("add", ".gitignore")
    repo.git("commit", "--quiet", "-m", "ignore logs")
    repo.write("new.txt", "new\n")
    repo.write("debug.log", "ignored\n")
    plain = repo.session("HEAD~4")
    assert "new.txt" not in [d.path for d in plain.files]
    assert plain.untracked_left_out() == ["new.txt"]

    session = Session.load(repo.client, "HEAD~4", include_untracked=True)
    assert "new.txt" in [d.path for d in session.files] and "debug.log" not in [d.path for d in session.files]
    assert session.untracked_left_out() == []
    plan = session.plan
    plan.assign(files_ids(session, "new.txt"), plan.add("Add new").key)
    session.finish(session.build())
    assert repo.show("HEAD", "new.txt") == "new\n" and repo.show("HEAD", "debug.log") is None


def test_unassigned_untracked_files_stay_untracked(repo: Repo) -> None:
    feature_repo(repo)
    repo.write("new.txt", "new\n")
    session = Session.load(repo.client, "HEAD~3", include_untracked=True)
    session.restore_old_commits()
    session.finish(session.build())
    assert repo.git("status", "--porcelain") == "?? new.txt"


def test_editing_a_file_the_range_doesnt_change(repo: Repo) -> None:
    from git_recommit.commands import Edit

    feature_repo(repo)
    session = repo.session("HEAD~3")
    session.restore_old_commits()
    # README is the same before and after the range.
    assert "README" not in [d.path for d in session.files]
    Edit(1, "README", b"stubbed\n").run(session)
    Edit(2, "README", b"readme\n").run(session)
    # And a file that's in neither.
    Edit(1, "scratch.txt", b"temporary\n").run(session)
    Edit(2, "scratch.txt", None).run(session)
    first, second, third = session.build()
    assert repo.show(first, "README") == "stubbed\n" and repo.show(second, "README") == "readme\n"
    assert repo.show(first, "scratch.txt") == "temporary\n" and repo.show(second, "scratch.txt") is None
    assert repo.show(third, "README") == "readme\n"
    assert session.left_uncommitted() == []


def test_rebuilding_commits_below_head_replays_the_later_ones(repo: Repo) -> None:
    feature_repo(repo)
    repo.git("commit", "--quiet", "--amend", "--no-edit", "--author=Someone Else <else@example.com>")
    later = repo.git("rev-parse", "HEAD")
    repo.write("a.py", "uncommitted\n")
    session = repo.session("HEAD~3..HEAD~1")
    assert [line.split(" ", 1)[1] for line in session.old_commits] == ["more wip", "wip"]
    assert session.describe_range().endswith("then replaying 1 later commit")
    session.restore_old_commits()
    plan = session.plan
    plan.commits[0].message = "wip, reworded"
    session.finish(session.build())
    assert repo.git("log", "--format=%s|%an", "-3").splitlines() == [
        "done|Someone Else",
        "more wip|Test",
        "wip, reworded|Test",
    ]
    # The later commit is the same but for its parent; the working tree too.
    assert repo.git("rev-parse", "HEAD^{tree}") == repo.git("rev-parse", f"{later}^{{tree}}")
    assert repo.git("status", "--porcelain") == "M a.py"


def test_below_head_every_change_must_be_assigned(repo: Repo) -> None:
    from git_recommit import writing

    feature_repo(repo)
    session = repo.session("HEAD~3..HEAD~1")
    session.restore_old_commits()
    session.plan.assign(files_ids(session, "notes.txt"), None)
    assert "later commits are replayed on" in (writing.blocker(session) or "")


RENAMED = "".join(f"line {i}\n" for i in range(1, 21))


def renamed_repo(repo: Repo) -> tuple[Session, list[int], int]:
    """old renamed to new, with line 3 changed; the session, the line changes'
    ids, and the rename's."""
    repo.commit("base", old=RENAMED)
    repo.commit("rename", old=None, new=RENAMED.replace("line 3\n", "three\n"))
    session = repo.session("HEAD~")
    (new,) = session.files
    assert new.meta_id is not None
    return session, [i for i in new.ids if i != new.meta_id], new.meta_id


def test_building_a_rename_apart_from_its_lines(repo: Repo) -> None:
    session, lines, rename = renamed_repo(repo)
    plan = session.plan
    plan.assign(lines, plan.add("Change line 3").key)
    plan.assign([rename], plan.add("Rename").key)
    first, second = session.build()
    assert repo.show(first, "old") == RENAMED.replace("line 3\n", "three\n") and repo.show(first, "new") is None
    assert repo.show(second, "new") == RENAMED.replace("line 3\n", "three\n") and repo.show(second, "old") is None


def test_building_a_rename_before_its_lines(repo: Repo) -> None:
    session, lines, rename = renamed_repo(repo)
    plan = session.plan
    plan.assign([rename], plan.add("Rename").key)
    plan.assign(lines, plan.add("Change line 3").key)
    first, second = session.build()
    assert repo.show(first, "new") == RENAMED and repo.show(first, "old") is None
    assert repo.show(second, "new") == RENAMED.replace("line 3\n", "three\n") and repo.show(second, "old") is None


def test_an_edit_follows_a_renamed_file(repo: Repo) -> None:
    session, lines, rename = renamed_repo(repo)
    plan = session.plan
    first, second = plan.add("Edit"), plan.add("Rename")
    plan.assign(lines, second.key)
    plan.assign([rename], second.key)
    (new,) = session.files
    session.set_edit(first.key, new, RENAMED.replace("line 20\n", "twenty\n").encode())
    shas = session.build()
    assert repo.show(shas[0], "old") == RENAMED.replace("line 20\n", "twenty\n")
    assert repo.show(shas[1], "new") == RENAMED.replace("line 3\n", "three\n").replace("line 20\n", "twenty\n")


def test_an_unassigned_rename_is_left_uncommitted(repo: Repo) -> None:
    session, lines, _ = renamed_repo(repo)
    plan = session.plan
    plan.assign(lines, plan.add("Change line 3").key)
    assert session.left_uncommitted() == ["new"]
    session.finish(session.build())
    assert repo.show("HEAD", "old") == RENAMED.replace("line 3\n", "three\n") and repo.show("HEAD", "new") is None
    # The rename is still an uncommitted change (new is added with
    # --intent-to-add), so the next recommit sees it.
    assert repo.git("status", "--porcelain") == "R old -> new"
    (new,) = repo.session("HEAD").files
    assert new.status == "R" and new.old_path == "old"


def test_building_a_rename_and_a_mode_change(repo: Repo) -> None:
    repo.commit("base", old=RENAMED)
    repo.git("mv", "old", "new")
    (repo.path / "new").chmod(0o755)
    repo.commit("rename")
    session = repo.session("HEAD~")
    plan = session.plan
    plan.add("Before")
    plan.assign(files_ids(session, "new"), plan.add("Rename").key)
    first, second = session.build()
    assert repo.git("ls-tree", first).endswith("\told") and "100644" in repo.git("ls-tree", first, "old")
    assert "100755" in repo.git("ls-tree", second, "new") and repo.show(second, "old") is None


def test_reloading_makes_files_from_the_new_diff(repo: Repo) -> None:
    repo.commit("base", f="1\n2\n")
    repo.commit("add b", f="1\n2\nb\n")
    session = repo.session("HEAD~")
    plan = session.plan
    plan.assign(files_ids(session, "f"), plan.add("first").key)
    assert session.states(session.files[0])[0].version == Version("100644", content=b"1\n2\nb\n")
    # The same change, with the same id, in another place.
    repo.write("f", "1\nb\n2\n")
    assert session.reload()
    assert session.states(session.files[0])[0].version == Version("100644", content=b"1\nb\n2\n")
