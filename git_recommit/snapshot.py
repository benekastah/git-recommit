"""What each changed file is in each new commit.

A file in a new commit is the file before the changes, with every change taken
by that commit or an earlier one applied. The user's edits (see plan.Edit) are
carried into later commits, by merging them with the changes those commits
take; where they overlap, the merge conflicts, and the user resolves it by
editing the file in the commit it conflicts in.
"""

from collections.abc import Callable, Container
from dataclasses import dataclass

from git_recommit.diff import FileDiff, apply
from git_recommit.plan import NewCommit

# (basis, ours, theirs) -> (merged, whether it conflicted)
Merge = Callable[[bytes, bytes, bytes], tuple[bytes, bool]]


@dataclass(frozen=True)
class Version:
    mode: str
    # A text file's content, or for a file changed as a whole, its blob.
    content: bytes | None = None
    sha: str | None = None


@dataclass(frozen=True)
class FileState:
    """A file in one new commit. version is None if the file isn't there."""

    version: Version | None
    # Whether carrying an edit into this commit conflicted.
    conflict: bool = False
    # Whether it differs from what the changes alone make it, due to edits.
    carried: bool = False
    # What an edit made in this commit is based on (see plan.Edit).
    basis: bytes = b""


def pure_version(diff: FileDiff, selected: Container[int]) -> Version | None:
    """The file with only the selected changes."""
    if diff.whole:
        take = diff.meta_id in selected
        mode, blob = (diff.new_mode, diff.new_blob) if take else (diff.old_mode, diff.old_blob)
        return None if mode is None else Version(mode, sha=blob)
    ids = diff.ids
    if diff.status == "A" and not any(i in selected for i in ids):
        return None
    if diff.status == "D" and all(i in selected for i in ids):
        return None
    if diff.status == "A" or (diff.status == "M" and diff.meta_id in selected):
        mode = diff.new_mode
    else:
        mode = diff.old_mode
    assert mode is not None
    return Version(mode, content=apply(diff, selected))


def _content(version: Version | None) -> bytes:
    return b"" if version is None or version.content is None else version.content


def file_states(diff: FileDiff, commits: list[NewCommit], assignment: dict[int, int], merge: Merge) -> list[FileState]:
    """The file in each of the commits, in order."""
    selected: set[int] = set()
    states = []
    edited = not diff.whole and any(diff.path in commit.edits for commit in commits)
    prev_pure = prev_eff = pure_version(diff, selected)
    prev_conflict = False
    for commit in commits:
        selected.update(i for i in diff.ids if assignment.get(i) == commit.key)
        pure = pure_version(diff, selected)
        if not edited:
            states.append(FileState(pure, basis=_content(pure)))
            continue
        # Carry the edits so far into this commit.
        if prev_eff == prev_pure:
            pre, conflict = pure, False
        elif pure == prev_pure:
            pre, conflict = prev_eff, prev_conflict
        else:
            merged, conflict = merge(_content(prev_pure), _content(prev_eff), _content(pure))
            conflict = conflict or prev_conflict
            mode = (pure or prev_eff or prev_pure or Version(diff.old_mode or diff.new_mode or "100644")).mode
            pre = None if pure is None and not merged and not conflict else Version(mode, content=merged)
        # A conflicted file is no basis for an edit, which resolves it.
        basis = _content(pure if conflict else pre)
        eff = pre
        if edit := commit.edits.get(diff.path):
            if edit.basis == basis:
                content, conflict = edit.content, False
            else:
                content, conflict = merge(edit.basis, edit.content, basis)
            mode = (pre or pure or Version(diff.new_mode or diff.old_mode or "100644")).mode
            eff = Version(mode, content=content)
        states.append(FileState(eff, conflict, eff != pure, basis))
        prev_pure, prev_eff, prev_conflict = pure, eff, conflict
    return states
