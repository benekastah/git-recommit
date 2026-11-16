"""git-recommit's additions to dev.py (see its docstring): `python dev.py types-windows`
type-checks as if on Windows, which CI runs too (project.yml)."""

import dev


def types_windows(args: list[str]) -> None:
    """basedpyright as if on Windows, for the code only Windows runs; `static` checks as if on
    Linux (see pyproject.toml)."""
    dev.venv()
    dev.run(dev.tool("basedpyright"), "--pythonplatform", "Windows", *args)


COMMANDS = {"types-windows": types_windows}
