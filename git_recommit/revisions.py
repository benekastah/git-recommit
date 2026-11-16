"""The commits a recommit rebuilds, and the working tree: reading the range,
the uncommitted changes as a commit, and what must not be in progress."""

import os
import shutil
import tempfile
from dataclasses import dataclass

from git_recommit.errors import RecommitError
from git_recommit.git import GitClient


def resolve_range(git: GitClient, spec: str) -> tuple[str, str]:
    """The (base, tip) commits of a range, read as git reads ranges:
    `<a>..<b>` is the commits in b but not a (so after their merge base);
    `<a>...<b>` those in either but not both, which must all be on one side;
    and `<a>` alone means `<a>..HEAD`. An end left out is HEAD.

    The commits to rebuild are those after base, up to and including tip,
    which must be HEAD. base can be HEAD too, to make commits of only the
    uncommitted changes.
    """
    separator = "..." if "..." in spec else ".." if ".." in spec else None
    a, _, b = spec.partition(separator) if separator else (spec, "", "HEAD")
    a, b = a or "HEAD", b or "HEAD"
    sha_a, sha_b = git.resolve_commit(a), git.resolve_commit(b)
    if separator == "...":
        if git.is_ancestor(sha_a, sha_b):  # including when they're the same
            base, tip = sha_a, sha_b
        elif git.is_ancestor(sha_b, sha_a):
            base, tip = sha_b, sha_a
        else:
            raise RecommitError(f"{spec} has commits on two branches, which can't be rebuilt as one")
    else:
        merge_base = git.run("merge-base", sha_a, sha_b, check=False)
        if not merge_base:
            raise RecommitError(f"{a} and {b} have no history in common")
        base, tip = merge_base, sha_b
    head = git.head()
    assert head is not None
    if base == tip and tip != head:
        raise RecommitError(f"{spec} has no commits")
    # A range can end below HEAD: the commits after it are replayed on the
    # new ones, as git rebase does.
    if tip != head and not git.is_ancestor(tip, head):
        raise RecommitError(f"{spec} isn't in HEAD's history, so it can't be rebuilt")
    if git.run("rev-list", "--merges", f"{base}..{head}"):
        raise RecommitError(f"{spec} has merge commits (or the commits after it do), which can't be rebuilt")
    return base, tip


def resolve_root(git: GitClient) -> tuple[str, str]:
    """The (base, tip) of every commit up to HEAD, as `git rebase --root`
    takes them: base is the empty tree, since there's no commit before."""
    head = git.head()
    if head is None:
        raise RecommitError("there are no commits yet")
    if git.run("rev-list", "--merges", head):
        raise RecommitError("HEAD's history has merge commits, which can't be rebuilt")
    return git.run("mktree", input=""), head


# What git keeps while another operation is in progress, and what to call it.
IN_PROGRESS = {
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase (or git am)",
}


def check_nothing_in_progress(git: GitClient, status: "WorktreeStatus | None" = None) -> None:
    """Refuse while another operation (a merge, cherry-pick, revert or rebase)
    is in progress, or the index has unresolved conflicts: taking the working
    tree as uncommitted changes would take the conflict markers, and writing
    (which resets the index) would end the operation. status, if given, is
    worktree_status's, which says if there are conflicts; without it, git
    ls-files finds them."""
    # With the paths worktree_commit and Session.finish need, so one git
    # process finds them all (GitClient keeps them).
    paths = git.git_paths(*IN_PROGRESS, "index", "index.lock")[: len(IN_PROGRESS)]
    for path, what in zip(paths, IN_PROGRESS.values(), strict=True):
        if os.path.exists(path):
            raise RecommitError(f"{what} is in progress; finish it or abort it first")
    unmerged = status.unmerged if status is not None else bool(git.run("ls-files", "--unmerged"))
    if unmerged:
        raise RecommitError("there are unresolved conflicts; resolve them first")


@dataclass(frozen=True)
class WorktreeStatus:
    # Whether the tracked files (or what's staged) have uncommitted changes.
    changed: bool
    # The untracked files that aren't ignored, from the top.
    untracked: list[str]
    # Whether the index has unresolved conflicts.
    unmerged: bool


def worktree_status(git: GitClient) -> WorktreeStatus:
    """The working tree's status, as worktree_commit takes it: as `git status`
    has it (with the user's config, e.g. submodule.<name>.ignore), but for a
    submodule whose only changes are inside it (untracked files, or tracked
    ones changed), which has nothing to commit until it commits them (even
    with untracked files included: they're the submodule's)."""
    # --no-optional-locks: don't refresh (and so write) the user's index.
    out = git.run_bytes(
        "--no-optional-locks", "status", "--porcelain=v2", "-z", "--no-renames", "--untracked-files=all"
    )
    changed = unmerged = False
    untracked = []
    # "? <path>" for an untracked file; for a changed one, "1 <XY> <submodule>
    # ..." (or "u ..." unmerged), where <submodule> is S<c><m><u> for a
    # submodule: "." for c if it hasn't a new commit. Paths are from the top.
    for entry in out.split(b"\0"):
        if entry.startswith(b"? "):
            untracked.append(entry[2:].decode(errors="surrogateescape"))
        elif entry.startswith(b"1 .M S."):
            continue
        elif entry:
            changed = True
            unmerged = unmerged or entry.startswith(b"u ")
    return WorktreeStatus(changed=changed, untracked=untracked, unmerged=unmerged)


def worktree_commit(git: GitClient, head: str, status: WorktreeStatus, include_untracked: bool = False) -> str | None:
    """A commit, on head, of the working tree's tracked files (and whatever is
    staged), as `git stash` makes; or None if they have no uncommitted changes
    (by status, from worktree_status). With include_untracked, as `git stash
    -u`: its untracked files too, but for ignored ones.

    It isn't on any branch; recommit uses it as the end of the diff, so that
    uncommitted changes can be assigned to new commits like committed ones.
    """
    if not status.changed and not (include_untracked and status.untracked):
        return None
    with tempfile.TemporaryDirectory() as scratch:
        # A copy of the real index, so `git add` sees what's staged, and only
        # reads the files that changed since it was written. copy2 keeps its
        # time, which git compares files' with: a file changed in the same
        # second as the index was written has to be read, as git knows.
        index = os.path.join(scratch, "index")
        shutil.copy2(git.git_paths("index")[0], index)
        env = {"GIT_INDEX_FILE": index}
        git.run("add", "--all" if include_untracked else "--update", "--", ":/", env=env)
        tree = git.run("write-tree", env=env)
    return git.run("commit-tree", tree, "-p", head, "-m", "Uncommitted changes")


def literal(path: str) -> str:
    """A pathspec for exactly the file at path (a path git reported, from the
    top of the worktree), not a pattern: a[1].txt mustn't match a1.txt."""
    return f":(top,literal){path}"
