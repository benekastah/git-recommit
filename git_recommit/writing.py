"""Writing the new commits: what the TUI's `w` and `git recommit write` share.

They differ only in how they ask the user: the TUI asks before writing, with
the warnings, and asks what to do when validation fails; the command line
prints the warnings, and stops at the first failure.
"""

from collections.abc import Callable

from git_recommit import validate
from git_recommit.errors import RecommitError
from git_recommit.session import Session

# Runs the validate command on (sha, subject) commits, and says what to do.
Validate = Callable[[list[tuple[str, str]]], validate.Outcome]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def blocker(session: Session) -> str | None:
    """Why the plan can't be written yet, if it can't."""
    if not session.plan.commits:
        return "there are no commits to make"
    if conflicts := session.conflicts():
        where = ", ".join(f"{c.path} in commit {session.plan.index(c.commit.key) + 1}" for c in conflicts)
        return f"resolve the conflicts first (by editing the file in that commit): {where}"
    if session.end != session.head and (left := session.left_uncommitted()):
        return (
            "the later commits are replayed on the last new commit, so it must end as the range does: assign every "
            f"change, and don't let edits last to the end (in {', '.join(left)})"
        )
    return None


def warnings(session: Session) -> list[str]:
    """What the user may not mean to write."""
    result = []
    if unassigned := session.unassigned():
        verb = "isn't" if len(unassigned) == 1 else "aren't"
        result.append(f"{_plural(len(unassigned), 'change')} {verb} assigned to a commit")
    result.extend(f"{path} will be left with uncommitted changes" for path in session.left_uncommitted())
    result.extend(
        f"commit {session.plan.index(commit.key) + 1} ({commit.subject}) will be empty"
        for commit in session.empty_commits()
    )
    result.extend(session.authorship_warnings())
    return result


def question(session: Session) -> str:
    """What writing does, to ask the user before."""
    old, new = len(session.old_commits), len(session.plan.commits)
    replace = f"Replace the {_plural(old, 'commit')}" if old else "Make"
    return f"{replace} with these {new}?"


def build(session: Session, run_validation: Validate | None) -> tuple[list[str], validate.Outcome]:
    """Make the new commits (moving no branch), and validate them, if there's
    something to validate with. Returns their ids, and what validating says
    to do."""
    if problem := blocker(session):
        raise RecommitError(problem)
    shas = session.build()
    if run_validation is None:
        return shas, validate.Outcome("ok")
    return shas, run_validation([(sha, c.subject) for sha, c in zip(shas, session.plan.commits, strict=True)])
