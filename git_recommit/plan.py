"""The new commits the user is building, and which changes each one takes."""

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Edit:
    """A change the user made by hand to a file in one new commit.

    Kept as the file's content before and after the edit, so that when the
    content before changes (e.g. the commit takes more changes), the edit can be
    merged into the new content, as a rebase would.
    """

    basis: bytes
    content: bytes


@dataclass(frozen=True)
class Author:
    name: str
    email: str
    # In a form GIT_AUTHOR_DATE takes, e.g. git's raw "<seconds> <offset>";
    # None for when the commit is written, as git commit --author does.
    date: str | None = None

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass
class NewCommit:
    # Stays the same when commits are added, removed or moved.
    key: int
    message: str
    edits: dict[str, Edit] = field(default_factory=dict)
    # Who wrote it and when, for one that starts from an old commit; for one
    # the user made, None, which means them, now.
    author: Author | None = None
    # The old commit's message, exactly, for one that starts from an old
    # commit: written as it is if the user didn't change message (which is
    # it without its final newline); if they did, writing runs the commit-msg
    # hook on the new one, as git does when a message is written.
    old_message: str | None = None

    @property
    def subject(self) -> str:
        return self.message.strip().partition("\n")[0]


class Plan:
    def __init__(self) -> None:
        self.commits: list[NewCommit] = []
        # Change id -> the key of the commit that takes it.
        self.assignment: dict[int, int] = {}
        self._next_key = 1

    def add(self, message: str, after: int | None = None, author: Author | None = None) -> NewCommit:
        """Add a commit after the one with key after, or last."""
        commit = NewCommit(self._next_key, message, author=author)
        self._next_key += 1
        self.commits.insert(len(self.commits) if after is None else self.index(after) + 1, commit)
        return commit

    def remove(self, key: int) -> None:
        """Remove a commit; the changes it took go back to being unassigned."""
        del self.commits[self.index(key)]
        self.assignment = {i: k for i, k in self.assignment.items() if k != key}

    def move(self, key: int, delta: int) -> None:
        """Move a commit delta places later (or earlier, if negative)."""
        index = self.index(key)
        new_index = max(0, min(len(self.commits) - 1, index + delta))
        self.commits.insert(new_index, self.commits.pop(index))

    def index(self, key: int) -> int:
        for index, commit in enumerate(self.commits):
            if commit.key == key:
                return index
        raise KeyError(key)

    def get(self, key: int) -> NewCommit:
        return self.commits[self.index(key)]

    def assign(self, ids: Iterable[int], key: int | None) -> None:
        """Have the commit with key take the changes, or with None, unassign them."""
        for i in ids:
            if key is None:
                self.assignment.pop(i, None)
            else:
                self.assignment[i] = key

    def count(self, key: int) -> int:
        return sum(1 for k in self.assignment.values() if k == key)
