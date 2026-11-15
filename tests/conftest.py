import pathlib

import pytest

from tests.helpers import Repo

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """An empty repository on main with a first commit (README). git, and git
    recommit run as a separate process, see an isolated git config and
    identity for the rest of the test."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[init]\n\tdefaultBranch = main\n")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_EDITOR": "true",
        "PYTHONPATH": str(ROOT),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GIT_SEQUENCE_EDITOR", raising=False)
    repo = Repo(tmp_path / "repo")
    repo.path.mkdir()
    repo.git("init", "--quiet")
    repo.commit("init", README="readme\n")
    return repo
