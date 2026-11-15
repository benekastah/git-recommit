"""Everything that changes the plan, as commands.

The TUI's keys and the command line's subcommands both run these, so they can
do the same things. A command is data (a frozen dataclass), naming commits by
their number in the plan (from 1) and changes by Targets, rather than by ids
that only mean something while the diff stays the same.
"""

import re
from dataclasses import dataclass
from typing import Literal

from git_recommit.diff import FileDiff, numbered
from git_recommit.errors import RecommitError
from git_recommit.plan import Author
from git_recommit.session import Session

Side = Literal["+", "-"]

# path:@<hunk>, or path:+<first>[-<last>] or path:-<first>[-<last>].
TARGET = re.compile(r"^(?P<path>.+?):(?:@(?P<hunk>\d+)|(?P<side>[+-])(?P<first>\d+)(?:-(?P<last>\d+))?)$")


@dataclass(frozen=True)
class Target:
    """Some of one file's changes: all of them; a hunk's, numbered from 1 (or
    0, the change to the file itself: its mode, or adding or deleting it when
    it's empty or binary); or the added (+) or removed (-) lines numbered from
    first to last, in the file after the changes or before them.

    Written `path`, `path:@2`, `path:+10-14` or `path:-3`."""

    path: str
    hunk: int | None = None
    side: Side | None = None
    first: int | None = None
    last: int | None = None

    @classmethod
    def parse(cls, spec: str) -> "Target":
        match = TARGET.match(spec)
        if match is None:
            return cls(spec)
        if match["hunk"] is not None:
            return cls(match["path"], hunk=int(match["hunk"]))
        first = int(match["first"])
        side: Side = "+" if match["side"] == "+" else "-"
        return cls(match["path"], side=side, first=first, last=int(match["last"] or first))

    def __str__(self) -> str:
        if self.hunk is not None:
            return f"{self.path}:@{self.hunk}"
        if self.side is not None:
            return f"{self.path}:{self.side}{_span(self.first, self.last)}"
        return self.path


def _span(first: int | None, last: int | None) -> str:
    return f"{first}" if first == last else f"{first}-{last}"


def file_diff(session: Session, path: str) -> FileDiff:
    for diff in session.files:
        if diff.path == path:
            return diff
    raise RecommitError(f"{path} isn't changed by the commits being rebuilt")


def resolve(session: Session, target: Target) -> list[int]:
    """The ids of the changes target names."""
    diff = file_diff(session, target.path)
    if target.hunk == 0:
        if diff.meta_id is None:
            raise RecommitError(f"{target.path} has no change to the file itself")
        return [diff.meta_id]
    if target.hunk is not None:
        if not 1 <= target.hunk <= len(diff.hunks):
            count = len(diff.hunks)
            raise RecommitError(f"{target.path} has {count} hunk{'s' if count != 1 else ''}")
        return diff.hunks[target.hunk - 1].ids
    if target.side is not None:
        assert target.first is not None and target.last is not None
        ids = [
            line.id
            for hunk in diff.hunks
            for line, old, new in numbered(hunk)
            if line.id is not None
            and line.kind == target.side
            and target.first <= ((new if target.side == "+" else old) or 0) <= target.last
        ]
        if not ids:
            what = "added" if target.side == "+" else "removed"
            raise RecommitError(f"there are no {what} lines {_span(target.first, target.last)} in {target.path}")
        return ids
    return diff.ids


def targets_for(session: Session, ids: list[int]) -> list[Target]:
    """Targets that name exactly the changes with ids: a file's path when it's
    all of the file's, else its lines (and its change to the file itself)."""
    wanted = set(ids)
    targets = []
    for diff in session.files:
        mine = [i for i in diff.ids if i in wanted]
        if not mine:
            continue
        if len(mine) == len(diff.ids):
            targets.append(Target(diff.path))
            continue
        if diff.meta_id in wanted:
            targets.append(Target(diff.path, hunk=0))
        sides: tuple[Side, Side] = ("+", "-")
        for side in sides:
            numbers = [
                (new if side == "+" else old) or 0
                for hunk in diff.hunks
                for line, old, new in numbered(hunk)
                if line.id in wanted and line.kind == side
            ]
            for first, last in _runs(numbers):
                targets.append(Target(diff.path, side=side, first=first, last=last))
    return targets


def _runs(numbers: list[int]) -> list[tuple[int, int]]:
    """Consecutive numbers as (first, last) runs: [1, 2, 3, 7] -> [(1, 3), (7, 7)]."""
    runs: list[tuple[int, int]] = []
    for n in numbers:
        if runs and runs[-1][1] == n - 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def commit_key(session: Session, number: int) -> int:
    """The key of the commit numbered number (from 1)."""
    if not 1 <= number <= len(session.plan.commits):
        raise RecommitError(f"there's no commit {number}")
    return session.plan.commits[number - 1].key


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


@dataclass(frozen=True)
class Result:
    """What a command did, to tell the user, and the number of the commit it
    left the user on, if any."""

    message: str
    commit: int | None = None


@dataclass(frozen=True)
class AddCommit:
    """Add a commit after commit number after (0: first; None: last)."""

    message: str
    after: int | None = None

    def run(self, session: Session) -> Result:
        message = self.message.strip()
        if not message:
            raise RecommitError("the message is empty")
        plan = session.plan
        if self.after is None or self.after == len(plan.commits):
            commit = plan.add(message)
        elif self.after == 0:
            commit = plan.add(message)
            plan.move(commit.key, -len(plan.commits))
        else:
            commit = plan.add(message, after=commit_key(session, self.after))
        number = plan.index(commit.key) + 1
        return Result(f"Created commit {number}.", number)


@dataclass(frozen=True)
class RemoveCommit:
    """Remove a commit; the changes it took become unassigned."""

    commit: int

    def run(self, session: Session) -> Result:
        commit = session.plan.get(commit_key(session, self.commit))
        session.plan.remove(commit.key)
        return Result(f"Deleted {commit.subject!r}; its changes are unassigned.")


@dataclass(frozen=True)
class MoveCommit:
    """Move a commit to be number to."""

    commit: int
    to: int

    def run(self, session: Session) -> Result:
        key = commit_key(session, self.commit)
        commit_key(session, self.to)
        session.plan.move(key, self.to - self.commit)
        return Result(f"Moved commit {self.commit} to {self.to}.", self.to)


@dataclass(frozen=True)
class Reword:
    """Change a commit's message."""

    commit: int
    message: str

    def run(self, session: Session) -> Result:
        commit = session.plan.get(commit_key(session, self.commit))
        message = self.message.strip()
        if not message:
            raise RecommitError("the message is empty")
        commit.message = message
        return Result(f"Reworded commit {self.commit}.", self.commit)


AUTHOR = re.compile(r"^(?P<name>[^<>]+?)\s*<(?P<email>[^<>]*)>$")


@dataclass(frozen=True)
class SetAuthor:
    """Make a commit's author "Name <email>" (dated when it's written), as
    git commit --author does, or with None, whoever writes it."""

    commit: int
    author: str | None

    def run(self, session: Session) -> Result:
        commit = session.plan.get(commit_key(session, self.commit))
        if self.author is None:
            commit.author = None
            return Result(f"Commit {self.commit} is now by whoever writes it ({session.me()}).", self.commit)
        match = AUTHOR.match(self.author.strip())
        if match is None:
            raise RecommitError(f"{self.author!r} isn't Name <email>")
        commit.author = Author(match["name"], match["email"])
        return Result(f"Commit {self.commit} is now by {commit.author}.", self.commit)


@dataclass(frozen=True)
class Assign:
    """Have a commit take the targets' changes, or with None, unassign them."""

    targets: tuple[Target, ...]
    commit: int | None

    def run(self, session: Session) -> Result:
        key = None if self.commit is None else commit_key(session, self.commit)
        ids = list(dict.fromkeys(i for target in self.targets for i in resolve(session, target)))
        session.plan.assign(ids, key)
        if self.commit is None:
            return Result(f"Unassigned {_plural(len(ids), 'change')}.")
        return Result(f"Assigned to {self.commit}: {_plural(len(ids), 'change')}.", self.commit)


@dataclass(frozen=True)
class Edit:
    """Make a text file content in a commit, by hand (carried into later
    commits). Content that's what the file already is in the commit changes
    nothing, so quitting an editor without saving is safe."""

    commit: int
    path: str
    content: bytes

    def run(self, session: Session) -> Result:
        key = commit_key(session, self.commit)
        diff = file_diff(session, self.path)
        if diff.whole:
            raise RecommitError(f"{self.path} isn't a text file, so it can't be edited")
        # E.g. an editor that saved Windows line endings, in a repository that
        # stores Unix ones.
        content = session.git.clean(self.path, self.content)
        if content == session.edit_text(key, diff):
            return Result(f"{self.path} is unchanged in commit {self.commit}.", self.commit)
        session.set_edit(key, diff, content)
        if self.path in session.plan.get(key).edits:
            return Result(f"Edited {self.path} in commit {self.commit}; later commits carry the edit.", self.commit)
        return Result(f"{self.path} in commit {self.commit} has no edits.", self.commit)


@dataclass(frozen=True)
class DropEdit:
    commit: int
    path: str

    def run(self, session: Session) -> Result:
        commit = session.plan.get(commit_key(session, self.commit))
        if commit.edits.pop(self.path, None) is None:
            raise RecommitError(f"commit {self.commit} doesn't edit {self.path}")
        return Result(f"Dropped commit {self.commit}'s edit of {self.path}.", self.commit)


@dataclass(frozen=True)
class Clear:
    """Remove every commit, to start from scratch."""

    def run(self, session: Session) -> Result:
        session.clear()
        return Result("Cleared every commit.")


@dataclass(frozen=True)
class Restore:
    """Go back to the commits being rebuilt, as they were at the start."""

    def run(self, session: Session) -> Result:
        session.restore_old_commits()
        return Result("Back to the commits being rebuilt.", 1 if session.plan.commits else None)


Command = AddCommit | RemoveCommit | MoveCommit | Reword | SetAuthor | Assign | Edit | DropEdit | Clear | Restore
ALL = (AddCommit, RemoveCommit, MoveCommit, Reword, SetAuthor, Assign, Edit, DropEdit, Clear, Restore)
