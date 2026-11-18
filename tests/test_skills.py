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
ALTERNATIVES = re.compile(r"([\w-]+(?:\|[\w-]+)+)")
SHELL_JOINERS = re.compile(r"\s(?:&&|\|\||;|\|)\s")
# What `edit` reads from, `git recommit edit 3 FILE < stub`.
REDIRECT = re.compile(r"\s<\s\S+")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def expand(command: str) -> list[str]:
    """command with each a|b|c as every one of them: a command for each."""
    match = ALTERNATIVES.search(command)
    if match is None:
        return [command]
    return [
        rest
        for choice in match[1].split("|")
        for rest in expand(command[: match.start()] + choice + command[match.end() :])
    ]


def recommit_commands(text: str) -> list[str]:
    """The `git recommit ...` command lines in text's code spans and fenced
    blocks, each as the arguments git recommit would get: placeholders filled
    in (with 1, which is a number, a message and a range), optional parts
    dropped, a|b|c as a command for each, a redirection or trailing comment
    cut."""
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
            part = PLACEHOLDER.sub("1", part).replace("...", "")
            words = (" ".join(each.removeprefix("git recommit").split()) for each in expand(part))
            commands.extend(command for command in words if command)
    return commands


def frontmatter(text: str) -> dict[str, str]:
    """The `key: value` lines between the --- lines, each on one line (not
    all of YAML: a value over more lines fails)."""
    match = FRONTMATTER.match(text)
    assert match, "no frontmatter"
    fields: dict[str, str] = {}
    for line in match[1].splitlines():
        key, colon, value = line.partition(": ")
        assert colon and key.replace("-", "_").isidentifier(), f"not `key: value`: {line!r}"
        fields[key] = value
    return fields


def parses(command: str) -> bool:
    """Whether git recommit takes command's arguments: a subcommand's, or
    (the range, the only argument that isn't a subcommand's) an option's."""
    args = shlex.split(command)
    first = args[0] if args else "-h"
    if first not in SUBCOMMANDS and first != "1" and not first.startswith("-"):
        return False  # taken for a range, though it's a misspelled subcommand
    try:
        (subcommand_parser() if first in SUBCOMMANDS else parser()).parse_args(args)
    except SystemExit as e:
        return e.code == 0  # -h is how the skill says to learn a command's options
    return True


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
        assert parses(command), f"`git recommit {command}` doesn't parse"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_a_skill_shows_every_subcommand_an_agent_uses(path: pathlib.Path) -> None:
    shown = {command.split()[0] for command in recommit_commands(path.read_text(encoding="utf-8"))}
    # The tui needs a person, and install-skills is for the person installing.
    assert set(SUBCOMMANDS) - {"tui", "install-skills"} <= shown


@pytest.mark.parametrize("command", ["tuii", "abortt", "assignn 3 a", "assign", "commit remove x"])
def test_a_command_that_isnt_one_doesnt_parse(command: str) -> None:
    assert not parses(command)


@pytest.mark.parametrize("command", ["start 1", "1 --validate 'pytest'", "-h", "commit remove 2", "tui -h"])
def test_a_command_that_is_one_parses(command: str) -> None:
    assert parses(command)


def test_alternatives_may_have_hyphens() -> None:
    assert expand("git recommit drop-edit|clear|restore") == [
        "git recommit drop-edit",
        "git recommit clear",
        "git recommit restore",
    ]


def test_commands_are_taken_from_code_spans_and_fenced_blocks() -> None:
    text = (
        "Use `git recommit start <range>` or `git recommit commit add|remove|move|reword|author [N]`.\n"
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
        "start 1",
        "commit add",
        "commit remove",
        "commit move",
        "commit reword",
        "commit author",
        "edit 3 src/api.py",
        "write --validate 'pytest'",
        "assign 3 src/api.py",
    ]
