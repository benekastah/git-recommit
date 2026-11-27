"""Starting from the commits being rebuilt (Session.restore_old_commits): a
new commit for each, with its message and author, taking the changes git
blame says it made, and the edits it needs to be as it was."""

import contextlib
import hashlib
import os
import re
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from git_recommit.diff import SUBMODULE_MODE, FileDiff, numbered
from git_recommit.errors import RecommitError
from git_recommit.plan import Author, Plan

if TYPE_CHECKING:
    from git_recommit.session import Session

# A line of `git blame --porcelain` that starts a line's entry: the commit,
# then the line's number in the commit, then its number in the file blamed.
BLAME_ENTRY = re.compile(rb"^([0-9a-f]{40,64}) \d+ (\d+)")


def restore(session: Session) -> None:
    """Start from the commits being rebuilt: one new commit for each, with
    its message and author, taking the changes it made (by git blame).

    Where a commit's changes overlap a later one's (e.g. it adds a line a
    later commit rewrites), the changes alone can't make the commit as it
    was, so it gets edits that do. Building the plan unchanged makes the
    same trees, but where a submodule changes, or a file's mode changes and
    changes back, which edits can't do (see docs/issues.md).
    """
    old = _old_commits(session)
    # The commit of the uncommitted changes, after them, isn't one of them.
    uncommitted: set[str] = set(old.pop().changed) if session.uncommitted else set()
    shas = [commit.sha for commit in old]
    session.plan = plan = Plan()
    keys: dict[str, int] = {}
    for commit in old:
        # Exactly as it is in the commit (git log would trim it), for
        # writing it unchanged; in the plan, without its final newline,
        # as messages the user writes are.
        raw = session.git.read_object(commit.sha, "commit")
        exact = raw.partition(b"\n\n")[2].decode(errors="surrogateescape")
        new = plan.add(exact.removesuffix("\n"), author=commit.author)
        new.old_message = exact
        keys[commit.sha] = new.key
    # A line last there in a commit (or base) was removed by the next one.
    removed_by = dict(zip([session.base, *shas], shas, strict=False))
    touched_by = _touched_by(old)
    trees = _trees(session, old)
    # Files old commits change that end as they started aren't in the diff;
    # add them, so the edits can reproduce them (but for submodules).
    changed = {path for diff in session.files for path in (diff.path, diff.old_path) if path is not None}
    for path in sorted(set(touched_by) - changed):
        with contextlib.suppress(RecommitError):
            session.add_unchanged(path)
    for diff in session.files:
        # The last commit to change the file takes what blame can't say
        # who made, as well as what blame doesn't cover.
        paths = {path for path in (diff.path, diff.old_path) if path is not None}
        touched = [sha for sha in shas if any(sha in touched_by.get(path, []) for path in paths)]
        last = keys.get(touched[-1]) if touched else None
        if diff.meta_id is not None:
            meta_by = _meta_by(
                diff, touched[-1] if touched else None, bool(paths & uncommitted), zip(shas, trees, strict=True)
            )
            plan.assign([diff.meta_id], None if meta_by is None else keys[meta_by])
        if diff.whole:
            continue
        if len(touched) == 1 and not paths & uncommitted:
            # Only one commit changed it, so it made every change.
            plan.assign([i for i in diff.ids if i != diff.meta_id], last)
            continue
        if paths & uncommitted and not _blamed(diff):
            # Nothing says which changes the uncommitted ones are, so none go
            # to a commit (the edits still make each as it was).
            continue
        for i, made_by in _made_by(session, diff, removed_by).items():
            plan.assign([i], keys.get(made_by or "", last))
    # Who made each change, for warning when a commit will be authored by
    # someone else (see authorship_warnings).
    authors = {commit.key: commit.author for commit in plan.commits}
    session.origin = {i: author for i, key in plan.assignment.items() if (author := authors[key]) is not None}
    _reproduce(session, trees)


def _meta_by(
    diff: FileDiff, last: str | None, uncommitted: bool, trees: Iterable[tuple[str, dict[str, str]]]
) -> str | None:
    """Which old commit (of the (sha, tree)s) made the file's change to
    itself, given the last to change it, if any: that one, but for a rename,
    which the first commit to have the new path made, or if none does, it's
    uncommitted. A file that changes as a whole and has uncommitted changes
    has them in that one change, which so stays uncommitted."""
    if diff.whole and uncommitted:
        return None
    if diff.old_path is None or diff.submodule:
        # (A submodule can't be edited, so its rename, which is all of its
        # change, stays with the last commit.)
        return last
    # For a file that changes as a whole, the rename is all of its change;
    # edits make the commits before the last as they were.
    return next((sha for sha, blobs in trees if diff.path in blobs), None)


@dataclass
class _OldCommit:
    sha: str
    author: Author
    # Each path it changes (from its parent, without finding renames), and
    # the blob it then has there, or None for none (or a submodule).
    changes: list[tuple[str, str | None]] = field(default_factory=list)

    @property
    def changed(self) -> list[str]:
        return [path for path, _ in self.changes]


def _touched_by(commits: list[_OldCommit]) -> dict[str, list[str]]:
    """For each path, the commits (in order) that changed it."""
    touched: dict[str, list[str]] = {}
    for commit in commits:
        for path in commit.changed:
            touched.setdefault(path, []).append(commit.sha)
    return touched


def _old_commits(session: Session) -> list[_OldCommit]:
    """The commits being rebuilt, oldest first, then the commit of the
    uncommitted changes, if any: with two git processes, not some for each."""
    span = session.tip if session.root else f"{session.base}..{session.tip}"
    shas = session.git.run_bytes("rev-list", "--reverse", span)
    if not shas:
        return []
    # Plumbing, rather than git log, whose output the user's config changes
    # (but for submodule.<name>.ignore, which plumbing reads too). --always:
    # even a commit that changes nothing.
    out = session.git.run_bytes(
        "diff-tree",
        "--stdin",
        "--always",
        "--ignore-submodules=none",
        "-r",
        "--root",
        "--no-renames",
        "--no-abbrev",
        "-z",
        "--format=%H%x00%an%x00%ae%x00%ad",
        "--date=raw",
        input=shas,
    )
    # Each commit's id, author's name, email and date, then for each path it
    # changes, ":<old mode> <new mode> <old blob> <new blob> <status>" (after
    # a line ending, for the first) and the path. A path is always just one
    # field, so it can't be mistaken for anything else.
    fields = iter(out.split(b"\0"))
    commits: list[_OldCommit] = []
    for entry in fields:
        entry = entry.lstrip(b"\n")
        if entry.startswith(b":"):
            _, new_mode, _, blob, status = entry.decode().split(" ")
            path = next(fields).decode(errors="surrogateescape")
            gone = status == "D" or new_mode == SUBMODULE_MODE
            commits[-1].changes.append((path, None if gone else blob))
        elif entry:
            name, email, date = (next(fields).decode(errors="surrogateescape") for _ in range(3))
            commits.append(_OldCommit(entry.decode(), Author(name, email, date)))
    return commits


def _made_by(session: Session, diff: FileDiff, removed_by: dict[str, str]) -> dict[int, str | None]:
    """For each of a text file's changes that was made before HEAD, the
    old commit that made it, by git blame, or None where blame and the
    diff disagree about the line (e.g. a blank line that moved)."""
    added = _blame(session, diff.path, reverse=False) if diff.new_blob else {}
    removed = _blame(session, diff.old_path or diff.path, reverse=True) if diff.old_blob else {}
    made_by: dict[int, str | None] = {}
    for hunk in diff.hunks:
        for line, old, new in numbered(hunk):
            if line.id is None:
                continue
            if line.kind == "+":
                # Added by an old commit, or after HEAD.
                by = added.get(new)
                if by == session.tip and session.uncommitted:
                    continue
                made_by[line.id] = by
            else:
                # Last there in base or an old commit, or in HEAD.
                there = removed.get(old)
                if there == session.head and session.uncommitted:
                    continue
                made_by[line.id] = removed_by.get(there or "")
    return made_by


# Whether an argument to git is Unicode, so a name that isn't UTF-8 can't be one.
WINDOWS = sys.platform == "win32"


def _blame(session: Session, path: str, reverse: bool) -> dict[int | None, str]:
    """For each line of the file (at tip, or with reverse, at base), the
    commit that last added it (or with reverse, the last that had it); none
    where git can't be given the name (see WINDOWS), which so is no one's."""
    if WINDOWS and not _is_utf8(path):
        return {}
    # From the root, the whole history; reverse blame isn't needed then, as
    # every file is added.
    span = session.tip if session.root else f"{session.base}..{session.tip}"
    # Every commit, even ones blame.ignoreRevsFile would have it pass over.
    args = ["blame", "--porcelain", "--ignore-revs-file=", *(["--reverse"] if reverse else []), span]
    lines: dict[int | None, str] = {}
    # blame takes a path from the current directory (not a pathspec), and
    # path is from the top.
    for entry in session.git.run_bytes(*args, "--", session.git.cdup() + path).split(b"\n"):
        if match := BLAME_ENTRY.match(entry):
            lines[int(match[2])] = match[1].decode()
    return lines


def _blamed(diff: FileDiff) -> bool:
    """Whether git can be asked who made the file's changes (see WINDOWS)."""
    return not WINDOWS or all(_is_utf8(path) for path in (diff.path, diff.old_path) if path is not None)


def _is_utf8(path: str) -> bool:
    """Whether path (git's bytes, decoded with surrogateescape) is UTF-8."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _reproduce(session: Session, trees: list[dict[str, str]]) -> None:
    """Give each commit of the plan the edits it needs to have each file
    (but submodules) as the old commit with trees[i] does."""
    # As an id's length says: SHA-1's are 40 hex digits, SHA-256's 64.
    object_format = "sha1" if len(session.base) == 40 else "sha256"

    def reproduce(diff: FileDiff) -> None:
        for index, (commit, blobs) in enumerate(zip(session.plan.commits, trees, strict=True)):
            # Only up to this commit: an edit here changes the later ones.
            state = session.states(diff, upto=index)[index]
            # Where the commit has the file: it gets the rename from the first
            # old commit to have the new path, so they agree.
            actual = blobs.get(state.path)
            version = state.version
            if actual is None:
                if version is not None:
                    session.set_edit(commit.key, diff, None)
                continue
            if version is not None and actual in (
                version.sha,
                None if version.content is None else blob_id(version.content, object_format),
            ):
                continue
            session.set_edit(commit.key, diff, session.git.blob(actual))

    # Each file is reproduced on its own (its edits are its own), so files
    # are done in parallel, which overlaps their git merge-file processes
    # (the rest holds the GIL).
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        list(pool.map(reproduce, [diff for diff in session.files if not diff.submodule]))
    # They finish in any order: put each commit's edits in the diff's, so
    # the plan is the same every time.
    order = {diff.path: index for index, diff in enumerate(session.files)}
    for commit in session.plan.commits:
        commit.edits = dict(sorted(commit.edits.items(), key=lambda item: order[item[0]]))


def _trees(session: Session, commits: list[_OldCommit]) -> list[dict[str, str]]:
    """Each file's blob in each commit: base's, as each commit changes them."""
    blobs: dict[str, str] = {}
    if not session.root:
        for entry in session.git.run_bytes("ls-tree", "-r", "-z", "--full-tree", session.base).split(b"\0"):
            if entry:
                meta, _, name = entry.partition(b"\t")
                _, kind, blob = meta.decode().split(" ")
                if kind == "blob":
                    blobs[name.decode(errors="surrogateescape")] = blob
    trees: list[dict[str, str]] = []
    for commit in commits:
        for path, change in commit.changes:
            if change is None:
                blobs.pop(path, None)
            else:
                blobs[path] = change
        trees.append(dict(blobs))
    return trees


def blob_id(content: bytes, object_format: str) -> str:
    """The id git gives a blob of content, without asking it."""
    return hashlib.new(object_format, b"blob %d\0" % len(content) + content).hexdigest()
