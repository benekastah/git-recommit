"""Running the --validate command on each new commit."""

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from git_recommit.git import GitClient


@dataclass(frozen=True)
class Outcome:
    # ok: every commit passed, or the user chose to go on anyway.
    # edit: the user wants to change the commit at index.
    # abort: the user wants to stop without rebuilding anything.
    action: Literal["ok", "edit", "abort"]
    index: int | None = None


def run(
    git: GitClient,
    command: str,
    commits: list[tuple[str, str]],
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> Outcome:
    """Run command in a scratch worktree with each (sha, subject) commit checked
    out in turn, asking the user what to do when it fails."""
    worktree = tempfile.mkdtemp(prefix="git-recommit-")
    try:
        git.run("worktree", "add", "--quiet", "--detach", worktree, commits[0][0])
        for index, (sha, subject) in enumerate(commits):
            git.run("-C", worktree, "checkout", "--quiet", "--detach", "--force", sha)
            say(f"==> [{index + 1}/{len(commits)}] {subject}: {command}")
            if subprocess.run(command, shell=True, cwd=worktree, check=False).returncode == 0:
                continue
            match _ask_on_failure(index, subject, ask):
                case "e":
                    return Outcome("edit", index)
                case "a":
                    return Outcome("abort")
        return Outcome("ok")
    finally:
        git.run("worktree", "remove", "--force", worktree, check=False)
        shutil.rmtree(worktree, ignore_errors=True)
        git.run("worktree", "prune", check=False)


def _ask_on_failure(index: int, subject: str, ask: Callable[[str], str]) -> str:
    while True:
        try:
            answer = ask(f"Validation failed for commit {index + 1} ({subject}). [e]dit, [a]bort or [p]roceed? ")
        except EOFError:
            return "a"
        answer = answer.strip().lower()[:1]
        if answer in ("e", "a", "p"):
            return answer
