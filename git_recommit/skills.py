"""Linking the agent skills in `git_recommit/skills/` into the directories agents read them from.

To support another agent, add an `Agent` to AGENTS: `install-skills` gets its
`--<name>` option from it.

dev_project.py imports this (for `dev.py install`) under any Python from 3.11, before .venv
exists, so it, `errors.py` and the package's `__init__.py` keep to syntax that runs there, and
import nothing else of the package. It only calls `found` and `packaged`: `is_link` uses
`os.path.isjunction`, new in 3.12.
"""

import enum
import os
import pathlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from git_recommit.errors import RecommitError

PACKAGED_SKILLS = pathlib.Path(__file__).parent / "skills"


@dataclass(frozen=True)
class Agent:
    """An agent that reads skills from a directory."""

    name: str
    """The name of its `--<name>` option, and what the result calls it."""
    skills_dir: Callable[[], pathlib.Path]
    """Where it reads the user's skills from."""
    marks: Callable[[], Sequence[pathlib.Path]]
    """Paths whose existence says the agent is in use here."""


def claude_config_dir() -> pathlib.Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return pathlib.Path(configured) if configured else pathlib.Path.home() / ".claude"


def codex_home() -> pathlib.Path:
    configured = os.environ.get("CODEX_HOME")
    return pathlib.Path(configured) if configured else pathlib.Path.home() / ".codex"


AGENTS = (
    Agent("claude", lambda: claude_config_dir() / "skills", lambda: [claude_config_dir()]),
    # Codex reads user skills from ~/.agents/skills, which other agents share.
    Agent(
        "codex",
        lambda: pathlib.Path.home() / ".agents" / "skills",
        lambda: [codex_home(), pathlib.Path.home() / ".agents"],
    ),
)


class InstallAction(enum.Enum):
    LINKED = "linked"
    REPLACED = "replaced"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class Installed:
    """A skill in one agent's skills directory."""

    agent: str
    skill: str
    path: str
    action: InstallAction


def is_link(path: pathlib.Path) -> bool:
    """Whether path is a symlink, or a Windows junction."""
    return path.is_symlink() or os.path.isjunction(path)


def make_link(link: pathlib.Path, target: pathlib.Path) -> None:
    """Make link a directory link to target: a symlink, or on Windows, where
    making one usually needs a privilege, a junction, which needs none."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # pyright: ignore[reportMissingImports]

        _winapi.CreateJunction(str(target), str(link))  # pyright: ignore[reportUnknownMemberType]


def plan_skill(agent: Agent, source: pathlib.Path, replace: bool) -> tuple[pathlib.Path, InstallAction | str]:
    """Where the skill directory source goes in agent's skills directory, and what
    installing it does there, or why it can't: a link to another directory (that still
    exists) is left alone unless replace, and a file or directory that isn't a link
    never is."""
    link = agent.skills_dir() / source.name
    if is_link(link):
        if link.resolve() == source.resolve():
            return link, InstallAction.UNCHANGED
        if replace or not link.exists():
            return link, InstallAction.REPLACED
        return link, f"{link} is a link to {link.resolve()}, not git-recommit's; rerun with --replace to replace it"
    if link.exists():
        return link, f"{link} exists and isn't a link; move it away to install git-recommit's {source.name} skill"
    return link, InstallAction.LINKED


def found() -> list[Agent]:
    """The agents in use here: those with a mark that exists."""
    return [a for a in AGENTS if any(p.exists() for p in a.marks())]


def packaged() -> list[pathlib.Path]:
    """The skill directories in the package."""
    return sorted(p for p in PACKAGED_SKILLS.iterdir() if p.is_dir())


def install(names: Sequence[str], replace: bool = False) -> list[Installed]:
    """Link every packaged skill into the skills directory of each agent in
    names, or, if there are none, of each agent found in use. Changes nothing if
    a link can't be made (see plan_skill); replace takes over links to other directories."""
    agents = [a for a in AGENTS if a.name in names] or found()
    if not agents:
        options = " or ".join(f"--{a.name}" for a in AGENTS)
        raise RecommitError(f"found no agent to install skills for; name one with {options}")
    plans = [(agent, source, *plan_skill(agent, source, replace)) for agent in agents for source in packaged()]
    if refusals := [what for *_, what in plans if isinstance(what, str)]:
        raise RecommitError("\n".join(refusals))
    installed: list[Installed] = []
    for agent, source, link, action in plans:
        assert isinstance(action, InstallAction)
        if action is not InstallAction.UNCHANGED and is_link(link) and link.resolve() == source.resolve():
            # Made since planning, through another agent's: they share a skills directory.
            action = InstallAction.UNCHANGED
        if action is not InstallAction.UNCHANGED:
            try:
                if action is InstallAction.REPLACED:
                    link.unlink() if link.is_symlink() else link.rmdir()
                link.parent.mkdir(parents=True, exist_ok=True)
                make_link(link, source)
            except OSError as e:
                raise RecommitError(f"can't link git-recommit's {source.name} skill at {link}: {e}") from e
        installed.append(Installed(agent.name, source.name, str(link), action))
    return installed
