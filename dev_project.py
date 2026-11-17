"""git-recommit's additions to dev.py (see its docstring): `python dev.py types-windows`
type-checks as if on Windows, which CI runs too (project.yml), and the man page's SYNOPSIS."""

import dev


def types_windows(args: list[str]) -> None:
    """basedpyright as if on Windows, for the code only Windows runs; `static` checks as if on
    Linux (see pyproject.toml)."""
    dev.venv()
    dev.run(dev.tool("basedpyright"), "--pythonplatform", "Windows", *args)


COMMANDS = {"types-windows": types_windows}

# As `git recommit --help` has it. `\-\-`: pandoc would make `--` a dash.
MAN_SYNOPSIS = {
    "git-recommit": "**git recommit** [**-C** *directory*] [**\\-\\-root**]"
    " [**\\-\\-validate** *command*] [**-u**] [**\\-\\-edit** *commit*] [*range*]"
}
