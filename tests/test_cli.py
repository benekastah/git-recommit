from tests.helpers import Repo


def test_needs_a_range_or_edit(repo: Repo) -> None:
    assert "give either a RANGE or --edit" in repo.recommit(expect_success=False).stderr
    assert "give either" in repo.recommit("HEAD~", "--edit", "HEAD", expect_success=False).stderr


def test_reports_errors_without_a_traceback(repo: Repo) -> None:
    result = repo.recommit("nope", expect_success=False)
    assert result.returncode == 1
    assert result.stderr == "git-recommit: nope isn't a commit\n"


def test_refuses_commits_that_change_nothing(repo: Repo) -> None:
    repo.commit("empty")
    assert "change nothing" in repo.recommit("HEAD~", expect_success=False).stderr


def test_rebuilding_from_the_command_line(repo: Repo) -> None:
    """What an agent would do: start, look, change the plan, write."""
    repo.commit("wip", a="a\n", b="b\n")
    repo.commit("more wip", a="a\na2\n")
    out = repo.recommit("start", "HEAD~2").stdout
    assert "1 wip" in out and "2 more wip" in out
    repo.recommit("clear")
    repo.recommit("commit", "add", "Add a")
    repo.recommit("commit", "add", "Add b")
    repo.recommit("assign", "1", "a")
    repo.recommit("assign", "2", "b")
    shown = repo.recommit("show").stdout
    assert "1 Add a" in shown and "2 Add b" in shown and "Unassigned: 0" in shown
    repo.recommit("write")
    assert repo.git("log", "--format=%s", "-2").splitlines() == ["Add b", "Add a"]
    assert repo.show("HEAD~", "a") == "a\na2\n" and repo.show("HEAD~", "b") is None
    assert repo.git("status", "--porcelain") == ""
    # The plan is gone once written.
    assert "no recommit in progress" in repo.recommit("show", expect_success=False).stderr


def test_show_json(repo: Repo) -> None:
    import json

    repo.commit("wip", a="a\n")
    repo.recommit("start", "HEAD~")
    shown = json.loads(repo.recommit("show", "--json").stdout)
    assert [c["message"] for c in shown["commits"]] == ["wip"]
    (file,) = shown["files"]
    assert file["path"] == "a"
    assert file["changes"] == [{"kind": "+", "old": None, "new": 1, "text": "a\n", "hunk": 1, "commit": 1}]
    assert shown["unassigned"] == 0


def test_editing_from_stdin_and_rewording(repo: Repo) -> None:
    repo.commit("wip", a="real\n")
    repo.recommit("start", "HEAD~")
    repo.recommit("commit", "add", "stub", "--after", "0")
    subprocess_result = repo.recommit("edit", "1", "a", input="stub\n")
    assert "Edited a in commit 1" in subprocess_result.stdout
    repo.recommit("commit", "reword", "2", "Real a")
    # The stub is carried into commit 2, where it overlaps a's real line.
    assert "resolve the conflicts first: a in commit 2" in repo.recommit("write", expect_success=False).stderr
    repo.recommit("edit", "2", "a", input="real\n")
    repo.recommit("write")
    assert repo.git("log", "--format=%s", "-2").splitlines() == ["Real a", "stub"]
    assert repo.show("HEAD~", "a") == "stub\n" and repo.show("HEAD", "a") == "real\n"


def test_one_recommit_at_a_time_and_abort(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    repo.recommit("start", "HEAD~")
    assert "already in progress" in repo.recommit("start", "HEAD~", expect_success=False).stderr
    repo.recommit("abort")
    repo.recommit("start", "HEAD~")


def test_write_stops_when_validation_fails(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    head = repo.git("rev-parse", "HEAD")
    repo.recommit("start", "HEAD~")
    result = repo.recommit("write", "--validate", "exit 3", expect_success=False)
    assert "validation failed for commit 1 (wip)" in result.stderr
    assert repo.git("rev-parse", "HEAD") == head
    # The plan is kept, to fix and write again.
    repo.recommit("show")


def test_bad_targets_and_commits_are_errors(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    repo.recommit("start", "HEAD~")
    assert "nope isn't changed" in repo.recommit("assign", "1", "nope", expect_success=False).stderr
    assert "there's no commit 5" in repo.recommit("assign", "5", "a", expect_success=False).stderr


def test_loading_a_plan_drops_edits_of_files_no_longer_changed(repo: Repo) -> None:
    repo.commit("wip", a="real\n", b="b\n")
    repo.recommit("start", "HEAD~")
    repo.recommit("edit", "1", "a", input="stub\n")
    (repo.path / "a").unlink()
    result = repo.recommit("show")
    assert "dropped commit 1's edit of a" in result.stderr
    again = repo.recommit("show")
    assert "edits a" not in again.stdout and again.stderr == ""
