import os
import pathlib

import pytest

# Before tests.helpers imports it, so pytest rewrites its asserts, as it does
# a plugin's.
pytest.register_assert_rewrite("git_client.testing")

from git_client.testing import RepoTemplates

from tests.helpers import GIT, Repo

# py-git-client's: each test's git (and git recommit's) gets a config of its
# own, with an identity, and none of the user's.
pytest_plugins = ["git_client.testing"]

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Before git recommit (in the tests' process, or one they start) first runs
# git, which finds it then.
os.environ["GIT_RECOMMIT_GIT"] = GIT


@pytest.fixture(autouse=True)
def _isolated(outside_any_repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs where git finds no repository, so a git that isn't
    given the test's can't change the one the tests run in; and git
    recommit, in a process of its own, imports this clone's."""
    monkeypatch.setenv("PYTHONPATH", str(ROOT))


def _make_template(directory: pathlib.Path) -> None:
    template = Repo(directory)
    template.git("init", "--quiet")
    template.commit("init", README="readme\n")


@pytest.fixture
def repo(tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str], repo_templates: RepoTemplates) -> Repo:
    """A repository on main with a first commit (README), copied from one
    made once per session (for each worker), since making it takes several
    git processes, which are slow to start on macOS and Windows."""
    repo_templates.build("repo", _make_template)
    return Repo(repo_templates.copy("repo", tmp_path / "repo"), capfd)
