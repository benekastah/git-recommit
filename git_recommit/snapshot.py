"""What each changed file is in each new commit.

A file in a new commit is the file before the changes, with every change taken
by that commit or an earlier one applied. The user's edits (see plan.Edit) are
carried into later commits, by merging them with the changes those commits
take; where they overlap, the merge conflicts, and the user resolves it by
editing the file in the commit it conflicts in.
"""

from collections.abc import Callable, Container
from dataclasses import dataclass

from git_recommit.diff import FileDiff, apply, looks_binary
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
    # Whether the conflict shows in the file as conflict markers. Without
    # them (a file that changes as a whole, or content git won't merge), the
    # file is as the edits carried in made it, and keeping that resolves it.
    markers: bool = False
    # Whether it differs from what the changes alone make it, due to edits.
    carried: bool = False
    # What an edit made in this commit is based on (see plan.Edit).
    basis: bytes = b""


def pure_version(diff: FileDiff, selected: Container[int]) -> Version | None:
    """The file with only the selected changes."""
    if diff.whole:
        take = diff.meta_id is not None and diff.meta_id in selected
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
    if diff.status == "A" or (diff.status in "MR" and diff.meta_id is not None and diff.meta_id in selected):
        mode = diff.new_mode
    else:
        mode = diff.old_mode
    assert mode is not None
    return Version(mode, content=apply(diff, selected))


def _content(version: Version | None) -> bytes:
    return b"" if version is None or version.content is None else version.content


def _blob(version: Version | None) -> bytes:
    """A version of a file that changes as a whole, as its edits keep it:
    its blob id (see plan.Edit)."""
    return b"" if version is None or version.sha is None else version.sha.encode()


def _text_version(mode: str, content: bytes) -> Version | None:
    return Version(mode, content=content)


def _blob_version(mode: str, sha: bytes) -> Version | None:
    return Version(mode, sha=sha.decode()) if sha else None


def _merge_whole(basis: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
    """Merge what can't be merged line by line (blob ids, or content git
    won't merge): a change on one side is taken, but changes on both
    conflict (keeping ours)."""
    if ours in (basis, theirs):
        return theirs, False
    if theirs == basis:
        return ours, False
    return ours, True


def _merge(merge: Merge, whole: bool, basis: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool, bool]:
    """The merge (with merge, or _merge_whole where it can't be line by
    line), whether it conflicted, and whether with conflict markers."""
    if whole or any(looks_binary(side) for side in (basis, ours, theirs)):
        return (*_merge_whole(basis, ours, theirs), False)
    merged, conflict = merge(basis, ours, theirs)
    return merged, conflict, conflict


def _carried_mode(
    diff: FileDiff, assigned: Version | None, prev_assigned: Version | None, prev_result: Version | None
) -> str:
    """The mode of the file in a commit that takes more changes than the one
    before it: the mode the changes it takes give it, if they change it, or else
    the one the earlier commits' edits gave it."""
    if assigned is not None and (prev_assigned is None or assigned.mode != prev_assigned.mode):
        return assigned.mode
    return (prev_result or assigned or prev_assigned or Version(diff.old_mode or diff.new_mode or "100644")).mode


def file_states(
    diff: FileDiff, commits: list[NewCommit], assignment: dict[int, int], merge: Merge, pure: Pure
) -> list[FileState]:
    """The file in each of the commits, in order, making the file with only
    some changes with pure (pure_version, or something that gives the same).

    For each commit: `assigned` is the file with only the changes assigned to
    it and earlier commits; `carried` is that with the edits of earlier
    commits carried in (merged); and `result` is that with this commit's own
    edit, if any, which is the file in the commit. A file that changes as a
    whole (e.g. a binary file) is merged by its blob ids, with _merge_whole
    rather than merge; so is content git takes for binary, which git
    merge-file refuses (as git's own merges do), even where attributes have
    it diffed line by line.
    """
    content, version = (_blob, _blob_version) if diff.whole else (_content, _text_version)

    taken: dict[int | None, list[int]] = {}
    for i in diff.ids:
        taken.setdefault(assignment.get(i), []).append(i)
    selected: frozenset[int] = frozenset()
    states: list[FileState] = []
    edited = any(diff.path in commit.edits for commit in commits)
    prev_assigned = prev_result = pure(selected)
    prev_conflict = prev_markers = False
    for commit in commits:
        selected = selected.union(taken.get(commit.key, ()))
        assigned = pure(selected)
        path = diff.path_with(selected)
        if not edited:
            states.append(FileState(assigned, path, basis=content(assigned)))
            continue
        # Carry the edits so far into this commit.
        if prev_result == prev_assigned:
            carried, conflict, markers = assigned, False, False
        elif assigned == prev_assigned:
            carried, conflict, markers = prev_result, prev_conflict, prev_markers
        else:
            merged, conflict, markers = _merge(
                merge, diff.whole, content(prev_assigned), content(prev_result), content(assigned)
            )
            # A conflict without markers keeps the edits carried in as they
            # are: a deletion too, which merged as content would be empty.
            kept_deletion = conflict and not markers and prev_result is None
            conflict, markers = conflict or prev_conflict, markers or prev_markers
            mode = _carried_mode(diff, assigned, prev_assigned, prev_result)
            gone = kept_deletion or (assigned is None and not merged and not conflict)
            carried = None if gone else version(mode, merged)
        # A conflicted file is no basis for an edit, which resolves it.
        basis = content(assigned if conflict else carried)
        result = carried
        if (edit := commit.edits.get(diff.path)) is not None and edit.content is None:
            # Deleting the file.
            result, conflict, markers = None, False, False
        elif edit is not None and edit.content is not None:
            if edit.basis == basis:
                edited_to, conflict, markers = edit.content, False, False
            else:
                edited_to, conflict, markers = _merge(merge, diff.whole, edit.basis, edit.content, basis)
            mode = edit.mode or (carried or assigned or Version(diff.new_mode or diff.old_mode or "100644")).mode
            result = version(mode, edited_to)
        states.append(FileState(result, path, conflict, markers, result != assigned, basis))
        prev_assigned, prev_result, prev_conflict, prev_markers = assigned, result, conflict, markers
    return states
