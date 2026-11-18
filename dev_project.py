"""git-recommit's additions to dev.py (see its docstring): `python dev.py types-windows`
type-checks as if on Windows, which CI runs too (project.yml), the man page's SYNOPSIS, and
the agent skills `install` links (by running `git recommit install-skills`)."""

import sys

import dev


def types_windows(args: list[str]) -> None:
    """basedpyright as if on Windows, for the code only Windows runs; `static` checks as if on
    Linux (see pyproject.toml)."""
    dev.venv()
    dev.run(dev.tool("basedpyright"), "--pythonplatform", "Windows", *args)


COMMANDS = {"types-windows": types_windows}


def after_install(replace: bool = False) -> None:
    """Link this clone's agent skills into each coding agent's skills directory, by running
    the clone's `git recommit install-skills`, which makes the links (a junction on Windows,
    where `install_links()`' symlinks wouldn't work) and stops at a link to another clone's
    skill unless `replace`. Having found no agent isn't a failure."""
    command = [dev.tool("git-recommit"), "install-skills", *(["--replace"] if replace else [])]
    if dev.run_code(*command) != 0:
        sys.exit(
            "install: couldn't link the agent skills (see above); `git recommit install-skills"
            " --replace` takes over a link to another clone's"
        )
