"""dev_project.py, the project's additions to dev.py, run through dev.py as the command line
does."""

import importlib
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_dev(monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]]) -> types.ModuleType:
    """dev.py, with its installing and running commands replaced by recording them."""
    monkeypatch.syspath_prepend(str(ROOT))
    dev = importlib.import_module("dev")
    monkeypatch.setattr(dev, "venv", lambda: ran.append(("venv",)))
    monkeypatch.setattr(dev, "run", lambda *args: ran.append(args))
    return dev


def test_types_windows_runs_basedpyright_as_if_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[str, ...]] = []
    dev = load_dev(monkeypatch, ran)
    dev.main(["types-windows", "git_recommit/git.py"])
    assert ran == [("venv",), (dev.tool("basedpyright"), "--pythonplatform", "Windows", "git_recommit/git.py")]


def test_install_links_the_skills_to_this_clones_for_each_agent_found(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    dev = load_dev(monkeypatch, [])
    monkeypatch.setattr(dev, "WINDOWS", False)
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    skills = ROOT / "git_recommit" / "skills" / "git-recommit"

    assert dev._project().install_links() == [
        (home / ".claude" / "skills" / "git-recommit", skills),
        (home / ".agents" / "skills" / "git-recommit", skills),
    ]


def test_install_links_no_skills_where_it_finds_no_agent(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    dev = load_dev(monkeypatch, [])
    monkeypatch.setattr(dev, "WINDOWS", False)

    assert dev._project().install_links() == []


def test_install_links_no_skills_on_windows_where_a_symlink_needs_a_privilege(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    dev = load_dev(monkeypatch, [])
    (home / ".claude").mkdir()
    monkeypatch.setattr(dev, "WINDOWS", True)

    assert dev._project().install_links() == []
