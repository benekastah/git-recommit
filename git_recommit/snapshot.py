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
# The changes selected -> the file with only them (see pure_version).
Pure = Callable[[frozenset[int]], "Version | None"]


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
    # Where it is (or would be): a renamed file's old path, until the
    # commit or an earlier one takes the rename.
    path: str
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
    if diff.status == "=" and diff.old_blob is None:
        # A file the range doesn't change, that isn't there at either end.
        return None
    if diff.status == "A" and not any(i in selected for i in ids):
        return None
    if diff.status == "D" and all(i in selected for i in ids):
        return None
    if diff.status == "A" or (diff.status in "MR" and diff.meta_id in selected):
        mode = diff.new_mode
    else:
        mode = diff.old_mode
    assert mode is not None
    return Version(mode, content=apply(diff, selected))


def _content(version: Version | None) -> bytes:
    return b"" if version is None or version.content is None else version.content


def file_states(
    diff: FileDiff, commits: list[NewCommit], assignment: dict[int, int], merge: Merge, pure: Pure
) -> list[FileState]:
    """The file in each of the commits, in order, making the file with only
    some changes with pure (pure_version, or something that gives the same).

    For each commit: `assigned` is the file with only the changes assigned to
    it and earlier commits; `carried` is that with the edits of earlier
    commits carried in (merged); and `result` is that with this commit's own
    edit, if any, which is the file in the commit.
    """
    taken: dict[int | None, list[int]] = {}
    for i in diff.ids:
        taken.setdefault(assignment.get(i), []).append(i)
    selected: frozenset[int] = frozenset()
    states = []
    edited = not diff.whole and any(diff.path in commit.edits for commit in commits)
    prev_assigned = prev_result = pure(selected)
    prev_conflict = False
    for commit in commits:
        selected = selected.union(taken.get(commit.key, ()))
        assigned = pure(selected)
        path = diff.path_with(selected)
        if not edited:
            states.append(FileState(assigned, path, basis=_content(assigned)))
            continue
        # Carry the edits so far into this commit.
        if prev_result == prev_assigned:
            carried, conflict = assigned, False
        elif assigned == prev_assigned:
            carried, conflict = prev_result, prev_conflict
        else:
            merged, conflict = merge(_content(prev_assigned), _content(prev_result), _content(assigned))
            conflict = conflict or prev_conflict
            mode = (
                assigned or prev_result or prev_assigned or Version(diff.old_mode or diff.new_mode or "100644")
            ).mode
            gone = assigned is None and not merged and not conflict
            carried = None if gone else Version(mode, content=merged)
        # A conflicted file is no basis for an edit, which resolves it.
        basis = _content(assigned if conflict else carried)
        result = carried
        if (edit := commit.edits.get(diff.path)) is not None and edit.content is None:
            # Deleting the file.
            result, conflict = None, False
        elif edit is not None and edit.content is not None:
            if edit.basis == basis:
                content, conflict = edit.content, False
            else:
                content, conflict = merge(edit.basis, edit.content, basis)
            mode = (carried or assigned or Version(diff.new_mode or diff.old_mode or "100644")).mode
            result = Version(mode, content=content)
        states.append(FileState(result, path, conflict, result != assigned, basis))
        prev_assigned, prev_result, prev_conflict = assigned, result, conflict
    return states
