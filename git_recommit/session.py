"""A recommit: the commits being rebuilt, the plan for the new ones, and
building them."""

import os
import shutil
import tempfile
from dataclasses import dataclass

from git_recommit import diff as diffs
from git_recommit.diff import FileDiff
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
from git_recommit.plan import Edit, NewCommit, Plan
from git_recommit.snapshot import FileState, Version, file_states, pure_version


def resolve_range(git: GitClient, spec: str) -> tuple[str, str]:
    """The (base, tip) commits of a range: `<base>` (to HEAD), `<a>..<b>` or
    `<a>...<b>`, whichever way round a and b are given.

    The commits to rebuild are those after base, up to and including tip,
    which is HEAD. base can be HEAD too, to make commits of only the
    uncommitted changes.
    """
    for separator in ("...", ".."):
        if separator in spec:
            a, _, b = spec.partition(separator)
            break
    else:
        a, b = spec, "HEAD"
    a, b = a or "HEAD", b or "HEAD"
    sha_a, sha_b = git.commit_sha(a), git.commit_sha(b)
    for rev, sha in ((a, sha_a), (b, sha_b)):
        if sha is None:
            raise RecommitError(f"{rev} isn't a commit")
    assert sha_a is not None and sha_b is not None
    if git.is_ancestor(sha_a, sha_b):  # including when they're the same
        base, tip = sha_a, sha_b
    elif git.is_ancestor(sha_b, sha_a):
        base, tip = sha_b, sha_a
    else:
        raise RecommitError(f"neither end of {spec} is an ancestor of the other")
    if tip != git.commit_sha("HEAD"):
        raise RecommitError(f"{spec} doesn't end at HEAD; only commits up to HEAD can be rebuilt")
    if git.run("rev-list", "--merges", f"{base}..{tip}"):
        raise RecommitError(f"{spec} has merge commits, which can't be rebuilt")
    return base, tip


def worktree_commit(git: GitClient, head: str) -> str | None:
    """A commit, on head, of the working tree's tracked files (and whatever is
    staged), as `git stash` makes; or None if they have no uncommitted changes.

    It isn't on any branch; recommit uses it as the end of the diff, so that
    uncommitted changes can be assigned to new commits like committed ones.
    """
    if not git.run("status", "--porcelain", "--untracked-files=no"):
        return None
    with tempfile.TemporaryDirectory() as scratch:
        # A copy of the real index, so `git add` sees what's staged, and only
        # reads the files that changed since it was written.
        index = os.path.join(scratch, "index")
        shutil.copyfile(git.run("rev-parse", "--path-format=absolute", "--git-path", "index"), index)
        env = {"GIT_INDEX_FILE": index}
        git.run("add", "--update", env=env)
        tree = git.run("write-tree", env=env)
    return git.run("commit-tree", tree, "-p", head, "-m", "Uncommitted changes")


@dataclass(frozen=True)
class Conflict:
    commit: NewCommit
    path: str


class Session:
    def __init__(self, git: GitClient, base: str, head: str, files: list[FileDiff], tip: str | None = None) -> None:
        """Rebuild the commits after base up to head, from files, the diff from
        base to tip: head, or with uncommitted changes, worktree_commit's
        commit on head."""
        self.git = git
        self.base = base
        self.head = head
        self.tip = tip or head
        self.files = files
        self.plan = Plan()
        self.old_commits = git.run("log", "--format=%h %s", f"{base}..{head}").splitlines()
        self.uncommitted = self.tip != head
        self._tip_tree = git.run("rev-parse", f"{self.tip}^{{tree}}")
        self._merges: dict[tuple[bytes, bytes, bytes], tuple[bytes, bool]] = {}

    @classmethod
    def load(cls, git: GitClient, spec: str) -> "Session":
        base, head = resolve_range(git, spec)
        tip = worktree_commit(git, head)
        if base == head and tip is None:
            raise RecommitError(f"{spec} has no commits in it, and there are no uncommitted changes")
        return cls(git, base, head, diffs.load(git, base, tip or head), tip)

    def reload(self) -> bool:
        """Load the diff again, to pick up changes the user made to the
        working tree or HEAD's branch since, and keep the assignments of the
        changes that are still there. Return whether anything changed."""
        try:
            base, head = resolve_range(self.git, self.base)
        except RecommitError as e:
            raise RecommitError(f"can't reload: {e}") from e
        tip = worktree_commit(self.git, head) or head
        tree = self.git.run("rev-parse", f"{tip}^{{tree}}")
        if head == self.head and tree == self._tip_tree:
            return False
        files = diffs.load(self.git, base, tip)
        if not files:
            raise RecommitError("can't reload: there are no changes any more")
        old_keys = diffs.change_keys(self.files)
        new_ids = {key: i for i, key in diffs.change_keys(files).items()}
        self.plan.assignment = {
            new_ids[old_keys[i]]: key for i, key in self.plan.assignment.items() if old_keys[i] in new_ids
        }
        self.head, self.tip, self.files, self._tip_tree = head, tip, files, tree
        self.old_commits = self.git.run("log", "--format=%h %s", f"{base}..{head}").splitlines()
        self.uncommitted = tip != head
        return True

    def merge(self, basis: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
        key = (basis, ours, theirs)
        if key not in self._merges:
            self._merges[key] = self.git.merge_file(basis, ours, theirs)
        return self._merges[key]

    def states(self, diff: FileDiff) -> list[FileState]:
        """The file in each new commit."""
        return file_states(diff, self.plan.commits, self.plan.assignment, self.merge)

    def edit_text(self, key: int, diff: FileDiff) -> bytes:
        """The file in the commit, as an edit starts from (with conflict markers
        if it conflicts). If the commit doesn't have the file, its end state
        (or if it ends up deleted, its start), to cut down to what the commit
        should have."""
        version = self.states(diff)[self.plan.index(key)].version
        if version is None:
            version = pure_version(diff, set(diff.ids)) or pure_version(diff, set())
        return b"" if version is None or version.content is None else version.content

    def set_edit(self, key: int, diff: FileDiff, content: bytes) -> None:
        """Make the file content in the commit (and carry that into later ones)."""
        if diff.whole:
            raise RecommitError(f"{diff.path} isn't a text file, so it can't be edited")
        commit = self.plan.get(key)
        state = self.states(diff)[self.plan.index(key)]
        commit.edits.pop(diff.path, None)
        # Even an edit that changes nothing resolves a conflict.
        if content != state.basis or state.conflict or state.version is None:
            commit.edits[diff.path] = Edit(state.basis, content)

    def conflicts(self) -> list[Conflict]:
        """Where carrying an edit conflicts, first in each file."""
        result = []
        for diff in self.files:
            if any(diff.path in commit.edits for commit in self.plan.commits):
                for commit, state in zip(self.plan.commits, self.states(diff), strict=True):
                    if state.conflict:
                        result.append(Conflict(commit, diff.path))
                        break
        return result

    def unassigned(self) -> list[int]:
        return [i for diff in self.files for i in diff.ids if i not in self.plan.assignment]

    def left_uncommitted(self) -> list[str]:
        """The files whose content in the last new commit differs from tip's, and
        so will be left as uncommitted changes."""
        if not self.plan.commits:
            return [diff.path for diff in self.files]
        return [diff.path for diff in self.files if self.states(diff)[-1].version != pure_version(diff, set(diff.ids))]

    def empty_commits(self) -> list[NewCommit]:
        return [commit for commit in self.plan.commits if not self.plan.count(commit.key) and not commit.edits]

    def build(self) -> list[str]:
        """Make the new commits (without moving any branch to them), and return
        their ids, oldest first."""
        if self.conflicts():
            raise RecommitError("resolve the conflicts first")
        states = {diff.path: self.states(diff) for diff in self.files}
        zero = "0" * len(self.base)
        blobs: dict[bytes, str] = {}

        def entry(path: str, version: Version | None) -> str:
            if version is None:
                return f"0 {zero}\t{path}\0"
            if version.sha is None:
                assert version.content is not None
                if version.content not in blobs:
                    blobs[version.content] = self.git.hash_blob(version.content)
                return f"{version.mode} {blobs[version.content]}\t{path}\0"
            return f"{version.mode} {version.sha}\t{path}\0"

        shas = []
        with tempfile.TemporaryDirectory() as scratch:
            env = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
            self.git.run("read-tree", self.base, env=env)
            parent = self.base
            previous = {diff.path: pure_version(diff, set()) for diff in self.files}
            for index, commit in enumerate(self.plan.commits):
                updates = []
                for diff in self.files:
                    version = states[diff.path][index].version
                    if version != previous[diff.path]:
                        updates.append(entry(diff.path, version))
                        previous[diff.path] = version
                if updates:
                    self.git.run("update-index", "-z", "--index-info", input="".join(updates), env=env)
                tree = self.git.run("write-tree", env=env)
                message = commit.message if commit.message.endswith("\n") else commit.message + "\n"
                parent = self.git.run("commit-tree", tree, "-p", parent, "-F", "-", input=message)
                shas.append(parent)
        return shas

    def finish(self, shas: list[str]) -> None:
        """Move the current branch (or HEAD) to the new commits, leaving the
        working tree as it is: what the new commits don't have is left as
        uncommitted changes."""
        new_tip = shas[-1] if shas else self.base
        self.git.run(
            "update-ref", "-m", f"recommit: rebuild {len(self.old_commits)} commits", "HEAD", new_tip, self.head
        )
        # Make the index match the new HEAD; the working tree keeps its changes.
        self.git.run("reset", "--quiet")
