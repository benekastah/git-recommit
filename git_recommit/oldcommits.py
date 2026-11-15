"""Starting from the commits being rebuilt (Session.restore_old_commits): a
new commit for each, with its message and author, taking the changes git
blame says it made, and the edits it needs to be as it was."""

import hashlib
import os
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from git_recommit.diff import FileDiff, numbered
from git_recommit.errors import RecommitError
from git_recommit.plan import Author, Plan
from git_recommit.revisions import changed_paths

if TYPE_CHECKING:
    from git_recommit.session import Session

# A line of `git blame --porcelain` that starts a line's entry: the commit,
# then the line's number in the commit, then its number in the file blamed.
BLAME_ENTRY = re.compile(rb"^([0-9a-f]{40,64}) \d+ (\d+)")


def restore(session: "Session") -> None:
    """Start from the commits being rebuilt: one new commit for each, with
    its message and author, taking the changes it made (by git blame).

    Where a commit's changes overlap a later one's (e.g. it adds a line a
    later commit rewrites), the changes alone can't make the commit as it
    was, so it gets edits that do. Building the plan unchanged makes the
    same trees, but where a submodule changes, which edits can't.
    """
    shas = session.git.run("rev-list", "--reverse", session.history).split()
    session.plan = plan = Plan()
    keys = {}
    for sha in shas:
        # Exactly as it is in the commit (git log would trim it), for
        # writing it unchanged; in the plan, without its final newline,
        # as messages the user writes are.
        exact = session.git.run_bytes("cat-file", "commit", sha).partition(b"\n\n")[2].decode(errors="surrogateescape")
        name, email, date = session.git.run("log", "-1", "--format=%an%x00%ae%x00%ad", "--date=raw", sha).split("\0")
        commit = plan.add(exact.removesuffix("\n"), author=Author(name, email, date))
        commit.old_message = exact
        keys[sha] = commit.key
    # A line last there in a commit (or base) was removed by the next one.
    removed_by = dict(zip([session.base, *shas], shas, strict=False))
    touched_by = _touched_by(session, shas)
    trees = [_blobs(session, sha) for sha in shas]
    # Files old commits change that end as they started aren't in the diff;
    # add them, so the edits can reproduce them (but for submodules).
    changed = {path for diff in session.files for path in (diff.path, diff.old_path) if path is not None}
    for path in sorted(set(touched_by) - changed):
        try:
            session.add_unchanged(path)
        except RecommitError:
            pass
    uncommitted = set(changed_paths(session.git, session.head, session.tip)) if session.uncommitted else set()
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


def _touched_by(session: "Session", shas: list[str]) -> dict[str, list[str]]:
    """For each path, the commits of shas (in order) that changed it."""
    touched: dict[str, list[str]] = {}
    for parent, sha in zip([session.base, *shas], shas, strict=False):
        for path in changed_paths(session.git, parent, sha):
            touched.setdefault(path, []).append(sha)
    return touched


def _made_by(session: "Session", diff: FileDiff, removed_by: dict[str, str]) -> dict[int, str | None]:
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


def _blame(session: "Session", path: str, reverse: bool) -> dict[int | None, str]:
    """For each line of the file (at tip, or with reverse, at base), the
    commit that last added it (or with reverse, the last that had it)."""
    # From the root, the whole history; reverse blame isn't needed then, as
    # every file is added.
    span = session.tip if session.root else f"{session.base}..{session.tip}"
    args = ["blame", "--porcelain", *(["--reverse"] if reverse else []), span]
    lines: dict[int | None, str] = {}
    for entry in session.git.run_bytes(*args, "--", path).split(b"\n"):
        if match := BLAME_ENTRY.match(entry):
            lines[int(match[2])] = match[1].decode()
    return lines


def _reproduce(session: "Session", trees: list[dict[str, str]]) -> None:
    """Give each commit of the plan the edits it needs to have each file
    (but submodules) as the old commit with trees[i] does."""
    object_format = session.git.run("rev-parse", "--show-object-format")

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


def _blobs(session: "Session", sha: str) -> dict[str, str]:
    """Each file's blob in the commit."""
    blobs = {}
    for entry in session.git.run_bytes("ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0"):
        if entry:
            meta, _, path = entry.partition(b"\t")
            _, kind, blob = meta.decode().split(" ")
            if kind == "blob":
                blobs[path.decode(errors="surrogateescape")] = blob
    return blobs


def blob_id(content: bytes, object_format: str) -> str:
    """The id git gives a blob of content, without asking it."""
    return hashlib.new(object_format, b"blob %d\0" % len(content) + content).hexdigest()
