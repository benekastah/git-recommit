"""Linking the agent skills in `git_recommit/skills/` into the directories agents read them from.

To support another agent, add an `Agent` to AGENTS: `install-skills` gets its
`--<name>` option from it.
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


def install_skill(agent: Agent, source: pathlib.Path) -> Installed:
    """Link the skill directory source into agent's skills directory."""
    link = agent.skills_dir() / source.name
    action = InstallAction.LINKED
    if is_link(link):
        if link.resolve() == source.resolve():
            return Installed(agent.name, source.name, str(link), InstallAction.UNCHANGED)
        action = InstallAction.REPLACED
    elif link.exists():
        raise RecommitError(
            f"{link} exists and isn't a link; move it away to install git-recommit's {source.name} skill"
        )
    try:
        if action is InstallAction.REPLACED:
            link.unlink() if link.is_symlink() else link.rmdir()
        link.parent.mkdir(parents=True, exist_ok=True)
        make_link(link, source)
    except OSError as e:
        raise RecommitError(f"can't link git-recommit's {source.name} skill at {link}: {e}") from e
    return Installed(agent.name, source.name, str(link), action)


def found() -> list[Agent]:
    """The agents in use here: those with a mark that exists."""
    return [a for a in AGENTS if any(p.exists() for p in a.marks())]


def packaged() -> list[pathlib.Path]:
    """The skill directories in the package."""
    return sorted(p for p in PACKAGED_SKILLS.iterdir() if p.is_dir())


def install(names: Sequence[str]) -> list[Installed]:
    """Link every packaged skill into the skills directory of each agent in
    names, or, if there are none, of each agent found in use."""
    agents = [a for a in AGENTS if a.name in names] or found()
    if not agents:
        options = " or ".join(f"--{a.name}" for a in AGENTS)
        raise RecommitError(f"found no agent to install skills for; name one with {options}")
    return [install_skill(agent, source) for agent in agents for source in packaged()]
