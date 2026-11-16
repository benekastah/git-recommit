import pathlib
import shutil

import pytest

from tests.helpers import Repo

ROOT = pathlib.Path(__file__).resolve().parent.parent


def isolate(monkeypatch: pytest.MonkeyPatch, directory: pathlib.Path) -> None:
    """Have git (and git recommit) see an isolated git config, with its
    global config in directory, and identity."""
    global_config = directory / "gitconfig"
    # Without auto-maintenance, which would otherwise start a `git maintenance
    # run --auto` after each commit, fetch, merge and push: hundreds of
    # processes over the tests, none of which they need.
    global_config.write_text("[init]\n\tdefaultBranch = main\n[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n")
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


@pytest.fixture(scope="session")
def template(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """The repository each test's repository starts as a copy of (see repo), made once
    (for each worker), since making it takes several git processes, which
    are slow to start on macOS and Windows."""
    directory = tmp_path_factory.mktemp("template")
    with pytest.MonkeyPatch.context() as monkeypatch:
        isolate(monkeypatch, directory)
        template = Repo(directory / "repo")
        template.path.mkdir()
        template.git("init", "--quiet")
        template.commit("init", README="readme\n")
    # Fewer files to copy for each test; the hooks directory stays.
    for sample in (template.path / ".git" / "hooks").glob("*.sample"):
        sample.unlink()
    return template.path


@pytest.fixture
def repo(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    template: pathlib.Path,
) -> Repo:
    """An empty repository on main with a first commit (README). git, and git
    recommit, see an isolated git config and identity for the rest of the
    test."""
    isolate(monkeypatch, tmp_path)
    repo = Repo(tmp_path / "repo", capfd)
    shutil.copytree(template, repo.path, symlinks=True)
    return repo
