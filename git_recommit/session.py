"""A recommit: the commits being rebuilt, the plan for the new ones, and
building them."""

import os
import subprocess
import tempfile
from dataclasses import dataclass

from git_recommit import oldcommits
from git_recommit.diff import FileDiff, change_keys, load_diff
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
from git_recommit.plan import Author, Edit, NewCommit, Plan
from git_recommit.revisions import (
    check_nothing_in_progress,
    literal,
    resolve_range,
    resolve_root,
    untracked_files,
    worktree_commit,
)
from git_recommit.snapshot import FileState, Version, file_states, pure_version


@dataclass(frozen=True)
class Conflict:
    commit: NewCommit
    path: str


class Session:
    """A recommit: the commits after `base` up to `head` (HEAD, when it was
    loaded) being rebuilt, from the diff from `base` to `tip`: `head`, or a
    commit of the uncommitted changes on it (see revisions.worktree_commit),
    whose tree (`_tip_tree`) says whether reloading finds anything new. And
    the plan for the new commits.
    """

    def __init__(
        self,
        git: GitClient,
        base: str,
        head: str,
        files: list[FileDiff],
        tip: str | None = None,
        include_untracked: bool = False,
        root: bool = False,
    ) -> None:
        """Rebuild the commits after base up to head, from files, the diff from
        base to tip: head, or with uncommitted changes, worktree_commit's
        commit on head (of untracked files too, with include_untracked)."""
        self.git = git
        # Whether rebuilding from the root commit: then base is the empty tree.
        self.root = root
        self.include_untracked = include_untracked
        self._untracked = untracked_files(git)
        self.base = base
        self.head = head
        self.tip = tip or head
        self.files = files
        self.plan = Plan()
        self.old_commits = git.run("log", "--format=%h %s", self.history).splitlines()
        self.uncommitted = self.tip != head
        self._tip_tree = git.run("rev-parse", f"{self.tip}^{{tree}}")
        self._merges: dict[tuple[bytes, bytes, bytes], tuple[bytes, bool]] = {}
        # The author of the old commit that made each change, where known.
        self.origin: dict[int, Author] = {}
        self._me: Author | None = None
        self._conflicts: tuple[tuple[object, ...], list[Conflict]] | None = None

    @classmethod
    def load(cls, git: GitClient, spec: str | None, include_untracked: bool = False) -> "Session":
        """The recommit of the commits spec names, or with None, every commit
        up to HEAD (as `git rebase --root`)."""
        check_nothing_in_progress(git)
        base, head = resolve_root(git) if spec is None else resolve_range(git, spec)
        tip = worktree_commit(git, head, include_untracked)
        if base == head and tip is None:
            raise RecommitError(f"{spec} has no commits in it, and there are no uncommitted changes")
        session = cls(git, base, head, load_diff(git, base, tip or head), tip, include_untracked, root=spec is None)
        return session

    @property
    def history(self) -> str:
        """The commits being rebuilt, as a revision range for git log."""
        return self.head if self.root else f"{self.base}..{self.head}"

    def describe_range(self) -> str:
        old = len(self.old_commits)
        commits = f"{old} commit{'s' if old != 1 else ''}"
        return f"Rebuilding {commits} from the root" if self.root else f"Rebuilding {commits} after {self.base[:7]}"

    def reload(self) -> bool:
        """Load the diff again, to pick up changes the user made to the
        working tree or HEAD's branch since, and keep the assignments of the
        changes that are still there. Return whether anything changed."""
        try:
            check_nothing_in_progress(self.git)
            base, head = resolve_root(self.git) if self.root else resolve_range(self.git, self.base)
        except RecommitError as e:
            raise RecommitError(f"can't reload: {e}") from e
        self._untracked = untracked_files(self.git)
        tip = worktree_commit(self.git, head, self.include_untracked) or head
        tree = self.git.run("rev-parse", f"{tip}^{{tree}}")
        if head == self.head and tree == self._tip_tree:
            return False
        files = load_diff(self.git, base, tip)
        if not files:
            raise RecommitError("can't reload: there are no changes any more")
        old_keys = change_keys(self.files)
        new_ids = {key: i for i, key in change_keys(files).items()}
        self.plan.assignment = {
            new_ids[old_keys[i]]: key for i, key in self.plan.assignment.items() if old_keys[i] in new_ids
        }
        self.origin = {new_ids[old_keys[i]]: author for i, author in self.origin.items() if old_keys[i] in new_ids}
        self.head, self.tip, self.files, self._tip_tree = head, tip, files, tree
        self.old_commits = self.git.run("log", "--format=%h %s", self.history).splitlines()
        self.uncommitted = tip != head
        return True

    def drop_stale_edits(self) -> list[str]:
        """Drop the edits of files the range no longer changes (e.g. after
        the user undid their changes while git recommit was suspended), which
        building would ignore, and say which."""
        changed = {diff.path for diff in self.files}
        dropped = []
        for number, commit in enumerate(self.plan.commits, start=1):
            for path in [path for path in commit.edits if path not in changed]:
                del commit.edits[path]
                dropped.append(f"dropped commit {number}'s edit of {path}, which the range no longer changes")
        return dropped

    def match(self, pathspec: str) -> list[str]:
        """The changed files a git pathspec matches, as git matches them (so
        relative to the current directory, with wildcards and magic)."""
        out = self.git.run_bytes(
            "diff-tree", "-r", "--no-renames", "--name-only", "-z", self.base, self.tip, "--", pathspec
        )
        return [path.decode(errors="surrogateescape") for path in out.split(b"\0") if path]

    def me(self) -> Author:
        """Who commits git recommit writes are by, unless they say otherwise."""
        if self._me is None:
            ident = self.git.run("var", "GIT_AUTHOR_IDENT")
            name, _, rest = ident.partition(" <")
            self._me = Author(name, rest.partition(">")[0])
        return self._me

    def authorship_warnings(self) -> list[str]:
        """For each commit that will be authored by one person but takes
        changes another made, a warning saying so."""
        warnings = []
        me = self.me()
        for number, commit in enumerate(self.plan.commits, start=1):
            author = commit.author or me
            others = {
                str(origin)
                for i, key in self.plan.assignment.items()
                if key == commit.key
                and (origin := self.origin.get(i)) is not None
                and (origin.name, origin.email) != (author.name, author.email)
            }
            if others:
                warnings.append(
                    f"commit {number} ({commit.subject}) will be authored by {author}, but has changes by "
                    f"{', '.join(sorted(others))}"
                )
        return warnings

    def head_moved(self) -> bool:
        """Whether HEAD has moved since the diff was loaded (e.g. the user
        committed while git recommit was suspended)."""
        return self.git.commit_sha("HEAD") != self.head

    def clear(self) -> None:
        """Start from scratch, with no commits."""
        self.plan = Plan()

    def restore_old_commits(self) -> None:
        """Start from the commits being rebuilt (see oldcommits.restore)."""
        oldcommits.restore(self)

    def merge(self, basis: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
        key = (basis, ours, theirs)
        if key not in self._merges:
            self._merges[key] = self.git.merge_file(basis, ours, theirs)
        return self._merges[key]

    def states(self, diff: FileDiff, upto: int | None = None) -> list[FileState]:
        """The file in each new commit, or in those up to the one at index
        upto (which is quicker, when the later ones don't matter)."""
        commits = self.plan.commits if upto is None else self.plan.commits[: upto + 1]
        return file_states(diff, commits, self.plan.assignment, self.merge)

    def edit_text(self, key: int, diff: FileDiff) -> bytes:
        """The file in the commit, as an edit starts from (with conflict markers
        if it conflicts). If the commit doesn't have the file, its end state
        (or if it ends up deleted, its start), to cut down to what the commit
        should have."""
        index = self.plan.index(key)
        version = self.states(diff, upto=index)[index].version
        if version is None:
            version = pure_version(diff, set(diff.ids)) or pure_version(diff, set())
        return b"" if version is None or version.content is None else version.content

    def set_edit(self, key: int, diff: FileDiff, content: bytes) -> None:
        """Make the file content in the commit (and carry that into later ones)."""
        if diff.whole:
            raise RecommitError(f"{diff.path} isn't a text file, so it can't be edited")
        commit = self.plan.get(key)
        index = self.plan.index(key)
        state = self.states(diff, upto=index)[index]
        commit.edits.pop(diff.path, None)
        # Even an edit that changes nothing resolves a conflict.
        if content != state.basis or state.conflict or state.version is None:
            commit.edits[diff.path] = Edit(state.basis, content)

    def _fingerprint(self) -> tuple[object, ...]:
        """Something that's the same as long as the plan is."""
        commits = tuple((c.key, tuple(c.edits.items())) for c in self.plan.commits)
        return (id(self.plan), id(self.files), commits, frozenset(self.plan.assignment.items()))

    def conflicts(self) -> list[Conflict]:
        """Where carrying an edit conflicts, first in each file. Kept until the
        plan changes, since the TUI asks on every redraw."""
        fingerprint = self._fingerprint()
        if self._conflicts is None or self._conflicts[0] != fingerprint:
            self._conflicts = (fingerprint, self._find_conflicts())
        return self._conflicts[1]

    def _find_conflicts(self) -> list[Conflict]:
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
        contents = list(
            dict.fromkeys(
                state.version.content
                for file_states in states.values()
                for state in file_states
                if state.version is not None and state.version.content is not None
            )
        )
        blobs = dict(zip(contents, self.git.hash_blobs(contents), strict=True))

        def entry(path: str, version: Version | None) -> bytes:
            # Paths were decoded with surrogateescape, which os.fsencode undoes,
            # so a name that isn't UTF-8 is written back as it was.
            if version is None:
                mode, blob = "0", zero
            elif version.sha is None:
                assert version.content is not None
                mode, blob = version.mode, blobs[version.content]
            else:
                mode, blob = version.mode, version.sha
            return f"{mode} {blob}\t".encode() + os.fsencode(path) + b"\0"

        # commit-tree only signs when asked to, unlike git commit.
        sign = ["-S"] if self.git.run("config", "--type=bool", "commit.gpgSign", check=False) == "true" else []
        shas = []
        with tempfile.TemporaryDirectory() as scratch:
            env = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
            self.git.run("read-tree", self.base, env=env)
            # From the root, the first new commit has no parent, as the root does.
            parent: str | None = None if self.root else self.base
            previous = {diff.path: pure_version(diff, set()) for diff in self.files}
            for position, commit in enumerate(self.plan.commits):
                updates = []
                for diff in self.files:
                    version = states[diff.path][position].version
                    if version != previous[diff.path]:
                        updates.append(entry(diff.path, version))
                        previous[diff.path] = version
                if updates:
                    self.git.run_bytes("update-index", "-z", "--index-info", input=b"".join(updates), env=env)
                tree = self.git.run("write-tree", env=env)
                message = self._message(position + 1, commit)
                parents = [] if parent is None else ["-p", parent]
                parent = self.git.run(
                    "commit-tree", tree, *parents, *sign, "-F", "-", input=message, env=_author_env(commit) | env
                )
                shas.append(parent)
        return shas

    def _message(self, number: int, commit: NewCommit) -> str:
        """The message to write for commit, number number. An old commit's
        message is kept exactly; a new or changed one is ended with a newline,
        and given to the commit-msg hook."""
        if commit.old_message is not None and commit.message == commit.old_message.removesuffix("\n"):
            return commit.old_message
        message = commit.message if commit.message.endswith("\n") else commit.message + "\n"
        return self._commit_msg_hook(number, commit, message)

    def _commit_msg_hook(self, number: int, commit: NewCommit, message: str) -> str:
        """message, after the repository's commit-msg hook (if any) has had
        its say, as git commit gives it, for a message that's new or changed.
        (commit-tree, which writes the commits, runs no hooks.)"""
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "COMMIT_EDITMSG")
            with open(path, "w", encoding="utf-8") as f:
                f.write(message)
            result = subprocess.run(
                ["git", "-C", self.git.directory, "hook", "run", "--ignore-missing", "commit-msg", "--", path],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                output = (result.stdout + result.stderr).strip()
                raise RecommitError(f"the commit-msg hook refused commit {number} ({commit.subject}):\n{output}")
            with open(path, encoding="utf-8") as f:
                return f.read()

    def finish(self, shas: list[str]) -> None:
        """Move the current branch (or HEAD) to the new commits, leaving the
        working tree as it is: what the new commits don't have is left as
        uncommitted changes."""
        check_nothing_in_progress(self.git)
        lock = self.git.run("rev-parse", "--path-format=absolute", "--git-path", "index.lock")
        if os.path.exists(lock):
            # Moving HEAD, then finding the index can't be reset, would leave
            # them out of step; so check first.
            raise RecommitError(f"{lock} exists: another git process is running, or one crashed (then remove it)")
        if not shas and self.root:
            raise RecommitError("from the root, there must be a commit to write")
        new_tip = shas[-1] if shas else self.base
        if self.head_moved():
            # Moving the branch would drop what moved it; say where the work is.
            raise RecommitError(
                f"HEAD moved while the new commits were being made, so they weren't used; they end at {new_tip}"
            )
        self.git.run(
            "update-ref", "-m", f"recommit: rebuild {len(self.old_commits)} commits", "HEAD", new_tip, self.head
        )
        # Make the index match the new HEAD; the working tree keeps its changes.
        self.git.run("reset", "--quiet")
        # Which would make a file the new commits don't have untracked, and so
        # no longer an uncommitted change (git recommit leaves untracked files
        # out): keep it known to git, as `git add --intent-to-add` does.
        if added := [diff.path for diff in self.files if self._only_in_tip(diff)]:
            self.git.run("add", "--intent-to-add", "--", *map(literal, added))

    def _only_in_tip(self, diff: FileDiff) -> bool:
        """Whether the file is at the tip but not in the last new commit (and
        wasn't untracked to start with, which it then goes back to being)."""
        if diff.path in self._untracked:
            return False
        final = self.states(diff)[-1].version if self.plan.commits else pure_version(diff, set())
        return final is None and pure_version(diff, set(diff.ids)) is not None

    def untracked_left_out(self) -> list[str]:
        """The untracked files that aren't ignored, but aren't included."""
        return [] if self.include_untracked else self._untracked


def _author_env(commit: NewCommit) -> dict[str, str]:
    """What tells commit-tree who wrote commit, and when (none: whoever runs
    git recommit, now)."""
    author = commit.author
    if author is None:
        return {}
    env = {"GIT_AUTHOR_NAME": author.name, "GIT_AUTHOR_EMAIL": author.email}
    if author.date is not None:
        env["GIT_AUTHOR_DATE"] = author.date
    return env
