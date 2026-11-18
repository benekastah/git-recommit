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


def test_the_man_page_has_the_synopsis_dev_project_gives_it() -> None:
    page = (ROOT / "man" / "git-recommit.1").read_text(encoding="utf-8")

    assert ".SH SYNOPSIS" in page
    assert "\\f[B]git recommit\\f[R]" in page


def run_code_with(monkeypatch: pytest.MonkeyPatch, code: int) -> tuple[types.ModuleType, list[tuple[str, ...]]]:
    ran: list[tuple[str, ...]] = []
    dev = load_dev(monkeypatch, ran)
    monkeypatch.setattr(dev, "run_code", lambda *args: ran.append(args) or code)
    return dev, ran


def test_install_links_the_agent_skills_by_running_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, ran = run_code_with(monkeypatch, 0)

    dev._project().after_install()

    assert ran == [(dev.tool("git-recommit"), "install-skills")]


def test_install_replace_takes_over_links_to_another_clones_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, ran = run_code_with(monkeypatch, 0)

    dev._project().after_install(replace=True)

    assert ran == [(dev.tool("git-recommit"), "install-skills", "--replace")]


def test_install_fails_when_the_skills_cant_be_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, _ = run_code_with(monkeypatch, 1)

    with pytest.raises(SystemExit, match=r"couldn't link the agent skills"):
        dev._project().after_install()
