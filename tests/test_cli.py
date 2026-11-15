import json

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
    assert "nope matches no file" in repo.recommit("assign", "1", "nope", expect_success=False).stderr
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


def test_works_whatever_the_locales_encoding(repo: Repo) -> None:
    # As on Windows, where text is cp1252 unless Python's UTF-8 mode is on.
    not_utf8 = {"PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"}
    repo.commit("Café crème", a="a\n")
    repo.git("commit", "--quiet", "--amend", "--no-edit", "--author=Zoë Ünïcode <z@example.com>")
    repo.recommit("start", "HEAD~", env=not_utf8)
    repo.recommit("show", env=not_utf8)
    shown = json.loads(repo.recommit("show", "--json", env=not_utf8).stdout)
    assert shown["commits"][0]["message"] == "Café crème"
    repo.recommit("commit", "add", "Ça va", env=not_utf8)
    repo.recommit("write", env=not_utf8)
    assert repo.git("log", "--format=%an|%s", "-2").splitlines() == ["Test|Ça va", "Zoë Ünïcode|Café crème"]


def pathspec_repo(repo: Repo) -> None:
    repo.commit(
        "wip",
        **{"top.txt": "t\n", "src/api.py": "a\n", "src/util.py": "u\n", "a1.txt": "1\n", "a[1].txt": "[1]\n"},
    )
    repo.recommit("start", "HEAD~")
    repo.recommit("clear")
    for message in ("A", "B", "C", "D"):
        repo.recommit("commit", "add", message)


def assigned(repo: Repo) -> dict[str, int | None]:
    shown = json.loads(repo.recommit("show", "--json").stdout)
    return {f["path"]: f["changes"][0]["commit"] for f in shown["files"]}


def test_targets_are_git_pathspecs(repo: Repo) -> None:
    pathspec_repo(repo)
    repo.recommit("assign", "1", "src")  # a directory: what's in it
    repo.recommit("assign", "2", "*.txt")  # a wildcard, as git matches them
    repo.recommit("assign", "3", ":(literal)a[1].txt")  # git's magic, too
    # Relative to the current directory, as in git (and -C adds up, as git's does).
    repo.recommit("-C", "src", "assign", "4", "api.py", ":/top.txt")
    assert assigned(repo) == {"a1.txt": 2, "a[1].txt": 3, "src/api.py": 4, "src/util.py": 1, "top.txt": 4}


def test_a_pathspec_must_match(repo: Repo) -> None:
    pathspec_repo(repo)
    assert "nothing* matches no file" in repo.recommit("assign", "1", "nothing*", expect_success=False).stderr
    # A hunk or lines are of one file.
    result = repo.recommit("assign", "1", "src:@1", expect_success=False)
    assert "src matches 2 files" in result.stderr


def test_edit_takes_a_pathspec(repo: Repo) -> None:
    pathspec_repo(repo)
    repo.recommit("-C", "src", "edit", "1", "api.py", input="stub\n")
    shown = json.loads(repo.recommit("show", "--json").stdout)
    assert shown["commits"][0]["edits"] == ["src/api.py"]
    repo.recommit("drop-edit", "1", "src/api*")
    assert json.loads(repo.recommit("show", "--json").stdout)["commits"][0]["edits"] == []


def test_start_include_untracked(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    repo.write("new.txt", "new\n")
    repo.recommit("start", "HEAD~")
    assert "1 untracked file left out (start with -u to include it)" in repo.recommit("show").stdout
    repo.recommit("abort")
    repo.recommit("start", "-u", "HEAD~")
    # Kept with the plan, for the commands after.
    shown = json.loads(repo.recommit("show", "--json").stdout)
    assert [f["path"] for f in shown["files"]] == ["a", "new.txt"]


def test_choosing_an_author(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    repo.recommit("start", "HEAD~")
    repo.recommit("commit", "author", "1", "Someone Else <else@example.com>")
    repo.recommit("write")
    assert repo.git("log", "-1", "--format=%an <%ae>") == "Someone Else <else@example.com>"
