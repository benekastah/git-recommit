import os
import sys

import pytest

from git_recommit import validate
from tests.helpers import Repo

# Passes in a commit that has the file "ok".
CHECK = f"{sys.executable} -c \"import os, sys; sys.exit(0 if os.path.exists('ok') else 1)\""


def commits(repo: Repo) -> list[tuple[str, str]]:
    return [
        (repo.commit("one", ok="1"), "one"),
        (repo.commit("two", ok=None), "two"),
        (repo.commit("three", ok="3"), "three"),
    ]


def test_every_commit_passing(repo: Repo) -> None:
    sha = repo.commit("good", ok="1")
    said: list[str] = []
    assert validate.run(repo.client, CHECK, [(sha, "good")], ask=lambda _: "", say=said.append) == validate.Outcome(
        "ok"
    )
    assert said == [f"==> [1/1] good: {CHECK}"]
    # The scratch worktree is gone.
    assert len(repo.git("worktree", "list").splitlines()) == 1


def test_a_failure_asks_what_to_do(repo: Repo) -> None:
    cs = commits(repo)
    asked: list[str] = []

    def answer(answers: list[str]) -> validate.Outcome:
        def ask(prompt: str) -> str:
            asked.append(prompt)
            return answers.pop(0)

        return validate.run(repo.client, CHECK, cs, ask=ask, say=lambda _: None)

    assert answer(["e"]) == validate.Outcome("edit", 1)
    assert "commit 2 (two)" in asked[0]
    assert answer(["what?", "abort"]) == validate.Outcome("abort")
    assert answer(["p"]) == validate.Outcome("ok")
    assert len(repo.git("worktree", "list").splitlines()) == 1


def test_end_of_input_aborts(repo: Repo) -> None:
    def ask(prompt: str) -> str:
        raise EOFError

    assert validate.run(repo.client, CHECK, commits(repo), ask=ask, say=lambda _: None) == validate.Outcome("abort")


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as a hook")
def test_the_command_and_hooks_see_the_users_environment(repo: Repo) -> None:
    sha = repo.commit("good", ok="1")
    seen = repo.path.parent / "seen"
    hook = repo.path / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho "hook [$GIT_LITERAL_PATHSPECS]" >> "{seen}"\n')
    hook.chmod(0o755)
    command = f'echo "command [$GIT_LITERAL_PATHSPECS]" >> "{seen}"'
    validate.run(repo.client, command, [(sha, "good")], ask=lambda _: "", say=lambda _: None)
    assert set(seen.read_text().splitlines()) == {"hook []", "command []"}
