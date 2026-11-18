"""The agent skills in `git_recommit/skills/` stay true to the command line they describe."""

import pathlib
import re
import shlex

import pytest

from git_recommit.cli import SUBCOMMANDS, parser, subcommand_parser
from tests.conftest import ROOT

SKILLS = ROOT / "git_recommit" / "skills"
SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))

CODE_SPAN = re.compile(r"`([^`\n]+)`")
OPTIONAL = re.compile(r"\[[^\]]*\]")
PLACEHOLDER = re.compile(r"<[^>]*>")
ALTERNATIVES = re.compile(r"(\w+)(?:\|\w+)+")
SHELL_JOINERS = re.compile(r"\s(?:&&|\|\||;|\|)\s")
# What `edit` reads from, `git recommit edit 3 FILE < stub`.
REDIRECT = re.compile(r"\s<\s\S+")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def recommit_commands(text: str) -> list[str]:
    """The `git recommit ...` command lines in text's code spans and fenced
    blocks, each as the arguments git recommit would get: placeholders filled
    in, optional parts dropped, the first of a|b|c taken, a redirection or
    trailing comment cut."""
    lines = [m.group(1) for m in CODE_SPAN.finditer(text)]
    fenced = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("```"):
            fenced = not fenced
        elif fenced:
            lines.append(line)
    commands: list[str] = []
    for line in lines:
        for part in SHELL_JOINERS.split(line.split(" #")[0]):
            if not part.startswith("git recommit"):
                continue
            part = REDIRECT.sub("", OPTIONAL.sub("", part))
            part = ALTERNATIVES.sub(r"\1", PLACEHOLDER.sub("x", part).replace("...", ""))
            if command := " ".join(part.removeprefix("git recommit").split()):
                commands.append(command)
    return commands


def frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER.match(text)
    assert match, "no frontmatter"
    return dict(line.split(": ", 1) for line in match[1].splitlines())


def test_the_skill_exists() -> None:
    assert (SKILLS / "git-recommit" / "SKILL.md").is_file()


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_a_skills_name_is_its_directorys_and_it_says_when_to_use_it(path: pathlib.Path) -> None:
    fields = frontmatter(path.read_text(encoding="utf-8"))
    assert fields["name"] == path.parent.name
    assert fields["description"].strip()


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_every_git_recommit_command_in_a_skill_parses(path: pathlib.Path) -> None:
    commands = recommit_commands(path.read_text(encoding="utf-8"))
    assert commands
    for command in commands:
        args = shlex.split(command)
        p = subcommand_parser() if args and args[0] in SUBCOMMANDS else parser()
        try:
            p.parse_args(args)
        except SystemExit as e:
            # -h is how the skill says to learn a command's options.
            assert e.code == 0, f"`git recommit {command}` doesn't parse"


def test_commands_are_taken_from_code_spans_and_fenced_blocks() -> None:
    text = (
        "Use `git recommit start <range>` or `git recommit commit add|remove [N] x`.\n"
        "```sh\n"
        "git recommit edit 3 src/api.py < stub.py    # by hand\n"
        "git recommit write --validate 'pytest' && git push\n"
        "```\n"
        "Not a command: git recommit start, or `git push`.\n"
        "1. In a list:\n\n"
        "   ```sh\n"
        "   git recommit assign 3 src/api.py\n"
        "   ```\n"
    )
    assert recommit_commands(text) == [
        "start x",
        "commit add x",
        "edit 3 src/api.py",
        "write --validate 'pytest'",
        "assign 3 src/api.py",
    ]
