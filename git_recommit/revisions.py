"""The commits a recommit rebuilds, and the working tree: reading the range,
the uncommitted changes as a commit, and what must not be in progress."""

import os
import shutil
import tempfile

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
    sha_a, sha_b = git.commit_sha(a), git.commit_sha(b)
    for rev, sha in ((a, sha_a), (b, sha_b)):
        if sha is None:
            raise RecommitError(f"{rev} isn't a commit")
    assert sha_a is not None and sha_b is not None
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
    head = git.commit_sha("HEAD")
    if base == tip and tip != head:
        raise RecommitError(f"{spec} has no commits")
    if tip != head:
        raise RecommitError(f"{spec} doesn't end at HEAD; only commits up to HEAD can be rebuilt")
    if git.run("rev-list", "--merges", f"{base}..{tip}"):
        raise RecommitError(f"{spec} has merge commits, which can't be rebuilt")
    return base, tip


# What git keeps while another operation is in progress, and what to call it.
IN_PROGRESS = {
    "MERGE_HEAD": "a merge",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase (or git am)",
}


def check_nothing_in_progress(git: GitClient) -> None:
    """Refuse while another operation (a merge, cherry-pick, revert or rebase)
    is in progress, or the index has unresolved conflicts: taking the working
    tree as uncommitted changes would take the conflict markers, and writing
    (which resets the index) would end the operation."""
    paths = git.run("rev-parse", "--path-format=absolute", *(a for name in IN_PROGRESS for a in ("--git-path", name)))
    for path, what in zip(paths.splitlines(), IN_PROGRESS.values(), strict=True):
        if os.path.exists(path):
            raise RecommitError(f"{what} is in progress; finish it or abort it first")
    if git.run("ls-files", "--unmerged"):
        raise RecommitError("there are unresolved conflicts; resolve them first")


def untracked_files(git: GitClient) -> list[str]:
    """The working tree's untracked files that aren't ignored, from the top."""
    out = git.run_bytes("ls-files", "--others", "--exclude-standard", "--full-name", "-z", ":/")
    return [path.decode(errors="surrogateescape") for path in out.split(b"\0") if path]


def worktree_commit(git: GitClient, head: str, include_untracked: bool = False) -> str | None:
    """A commit, on head, of the working tree's tracked files (and whatever is
    staged), as `git stash` makes; or None if they have no uncommitted changes.
    With include_untracked, as `git stash -u`: its untracked files too, but
    for ignored ones.

    It isn't on any branch; recommit uses it as the end of the diff, so that
    uncommitted changes can be assigned to new commits like committed ones.
    """
    untracked = "--untracked-files=all" if include_untracked else "--untracked-files=no"
    # --no-optional-locks: don't refresh (and so write) the user's index.
    if not git.run("--no-optional-locks", "status", "--porcelain", untracked):
        return None
    with tempfile.TemporaryDirectory() as scratch:
        # A copy of the real index, so `git add` sees what's staged, and only
        # reads the files that changed since it was written. copy2 keeps its
        # time, which git compares files' with: a file changed in the same
        # second as the index was written has to be read, as git knows.
        index = os.path.join(scratch, "index")
        shutil.copy2(git.run("rev-parse", "--path-format=absolute", "--git-path", "index"), index)
        env = {"GIT_INDEX_FILE": index}
        git.run("add", "--all" if include_untracked else "--update", "--", ":/", env=env)
        tree = git.run("write-tree", env=env)
    return git.run("commit-tree", tree, "-p", head, "-m", "Uncommitted changes")


def changed_paths(git: GitClient, old: str, new: str) -> list[str]:
    """The paths that differ between two commits (or trees)."""
    out = git.run_bytes("diff-tree", "-r", "--no-renames", "--name-only", "-z", old, new)
    return [path.decode(errors="surrogateescape") for path in out.split(b"\0") if path]


def literal(path: str) -> str:
    """A pathspec for exactly the file at path (a path git reported, from the
    top of the worktree), not a pattern: a[1].txt mustn't match a1.txt."""
    return f":(top,literal){path}"
