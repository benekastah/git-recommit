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
