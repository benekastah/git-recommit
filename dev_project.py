"""git-recommit's additions to dev.py (see its docstring): `python dev.py types-windows`
type-checks as if on Windows, which CI runs too (project.yml), the man page's SYNOPSIS, and
the agent skills `install` links."""

from pathlib import Path

import dev


def types_windows(args: list[str]) -> None:
    """basedpyright as if on Windows, for the code only Windows runs; `static` checks as if on
    Linux (see pyproject.toml)."""
    dev.venv()
    dev.run(dev.tool("basedpyright"), "--pythonplatform", "Windows", *args)


COMMANDS = {"types-windows": types_windows}


def install_links() -> list[tuple[Path, Path]]:
    """The agent skills, linked into each agent found in use to this clone's, as `git recommit
    install-skills` does to the installed package's. None on Windows, where a symlink needs a
    privilege: there, that command makes a junction instead."""
    if dev.WINDOWS:
        return []
    # Not at the top: this file is loaded before .venv exists, under any Python from 3.11, and
    # git_recommit/skills.py is written to import there (see pyproject.toml).
    from git_recommit import skills

    return [(agent.skills_dir() / source.name, source) for agent in skills.found() for source in skills.packaged()]


# As `git recommit --help` has it. `\-\-`: pandoc would make `--` a dash.
MAN_SYNOPSIS = {
    "git-recommit": "**git recommit** [**-C** *directory*] [**\\-\\-root**]"
    " [**\\-\\-validate** *command*] [**-u**] [**\\-\\-edit** *commit*] [*range*]"
}
