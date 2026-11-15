"""A recommit: the commits being rebuilt, the plan for the new ones, and
building them."""

import os
import posixpath
import subprocess
import tempfile
from dataclasses import dataclass

from git_recommit import oldcommits
from git_recommit.diff import SPECIAL_MODES, FileDiff, change_keys, load_diff, split_lines
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
    """A recommit: the commits after `base` up to `end` being rebuilt, from
    the diff from `base` to `tip`: `end`, or (when the range ends at HEAD) a
    commit of the uncommitted changes on it (see revisions.worktree_commit),
    whose tree (`_tip_tree`) says whether reloading finds anything new. And
    the plan for the new commits. `head` is HEAD, when it was loaded: `end`,
    or a later commit, when the commits after the range are replayed.
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
        end: str | None = None,
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
        self.end = end or head
        self.tip = tip or self.end
        self.files = files
        self.plan = Plan()
        self.old_commits = git.run("log", "--format=%h %s", self.history).splitlines()
        self.uncommitted = self.tip != self.end
        self._tip_tree = git.run("rev-parse", f"{self.tip}^{{tree}}")
        self._merges: dict[tuple[bytes, bytes, bytes], tuple[bytes, bool]] = {}
        # For each file, its versions with only some changes (see states).
        self._pure: dict[str, dict[frozenset[int], Version | None]] = {}
        # The author of the old commit that made each change, where known.
        self.origin: dict[int, Author] = {}
        # Files the range doesn't change that commits edit (see add_unchanged).
        self.unchanged: set[str] = set()
        self._me: Author | None = None
        self._conflicts: tuple[tuple[object, ...], list[Conflict]] | None = None

    @classmethod
    def load(cls, git: GitClient, spec: str | None, include_untracked: bool = False) -> "Session":
        """The recommit of the commits spec names, or with None, every commit
        up to HEAD (as `git rebase --root`)."""
        check_nothing_in_progress(git)
        base, end = resolve_root(git) if spec is None else resolve_range(git, spec)
        head = git.commit_sha("HEAD")
        assert head is not None
        # Uncommitted changes come after HEAD, so only a range ending there takes them.
        tip = worktree_commit(git, end, include_untracked) if end == head else None
        if base == end and tip is None:
            raise RecommitError(f"{spec} has no commits in it, and there are no uncommitted changes")
        files = load_diff(git, base, tip or end)
        return cls(git, base, head, files, tip, include_untracked, root=spec is None, end=end)

    def later(self) -> list[str]:
        """The commits after the range, up to HEAD, which writing replays."""
        return (
            [] if self.end == self.head else self.git.run("rev-list", "--reverse", f"{self.end}..{self.head}").split()
        )

    @property
    def history(self) -> str:
        """The commits being rebuilt, as a revision range for git log."""
        return self.end if self.root else f"{self.base}..{self.end}"

    def describe_range(self) -> str:
        old = len(self.old_commits)
        commits = f"{old} commit{'s' if old != 1 else ''}"
        what = f"Rebuilding {commits} from the root" if self.root else f"Rebuilding {commits} after {self.base[:7]}"
        if later := len(self.later()):
            what += f", then replaying {later} later commit{'s' if later != 1 else ''}"
        return what

    def reload(self) -> bool:
        """Load the diff again, to pick up changes the user made to the
        working tree or HEAD's branch since, and keep the assignments of the
        changes that are still there. Return whether anything changed."""
        try:
            check_nothing_in_progress(self.git)
            if self.end != self.head:
                # A range below HEAD stays as it is, but for what comes after.
                base, end, head = self.base, self.end, self.git.commit_sha("HEAD") or self.head
                if not self.git.is_ancestor(end, head):
                    raise RecommitError("HEAD isn't after the commits being rebuilt any more")
                tip = end
            else:
                base, end = resolve_root(self.git) if self.root else resolve_range(self.git, self.base)
                head = end
                tip = worktree_commit(self.git, head, self.include_untracked) or head
        except RecommitError as e:
            raise RecommitError(f"can't reload: {e}") from e
        self._untracked = untracked_files(self.git)
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
        self.head, self.end, self.tip, self.files, self._tip_tree = head, end, tip, files, tree
        # Change ids mean other changes in the new diff.
        self._pure = {}
        changed = {diff.path for diff in files}
        for path in sorted(self.unchanged - changed):
            self.unchanged.discard(path)
            self.add_unchanged(path)
        self.old_commits = self.git.run("log", "--format=%h %s", self.history).splitlines()
        self.uncommitted = tip != end
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
        relative to the current directory, with wildcards and magic). A
        renamed file matches by either path, and is given by its new one."""
        out = self.git.run_bytes(
            "diff-tree", "-r", "--no-renames", "--name-only", "-z", self.base, self.tip, "--", pathspec
        )
        new_paths = {diff.old_path: diff.path for diff in self.files if diff.old_path is not None}
        paths = (path.decode(errors="surrogateescape") for path in out.split(b"\0") if path)
        return list(dict.fromkeys(new_paths.get(path, path) for path in paths))

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

    def plain_path(self, path: str) -> str:
        """path, relative to the current directory (or with :/, the top), as a
        path from the top, as git takes a path that isn't a pattern."""
        if path.startswith(":/"):
            joined = path[2:]
        else:
            joined = self.git.run("rev-parse", "--show-prefix") + path
        normal = posixpath.normpath(joined.replace(os.sep, "/"))
        if normal.startswith("../") or normal in (".", ".."):
            raise RecommitError(f"{path} is outside the repository")
        return normal

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
        # Making a file with only some changes is most of the work, and the
        # same changes come up again and again (e.g. for each commit, when
        # starting from the old ones, and on each redraw). So keep the
        # versions the last call made, to reuse: only those, so what's kept
        # stays the size of one call's.
        kept = self._pure.get(diff.path, {})
        made: dict[frozenset[int], Version | None] = {}

        def pure(selected: frozenset[int]) -> Version | None:
            if selected not in made:
                made[selected] = kept[selected] if selected in kept else pure_version(diff, selected)
            return made[selected]

        states = file_states(diff, commits, self.plan.assignment, self.merge, pure)
        self._pure[diff.path] = made
        return states

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

    def set_edit(self, key: int, diff: FileDiff, content: bytes | None) -> None:
        """Make the file content in the commit, or with None, delete it (and
        carry that into later ones)."""
        if diff.whole:
            raise RecommitError(f"{diff.path} isn't a text file, so it can't be edited")
        commit = self.plan.get(key)
        index = self.plan.index(key)
        state = self.states(diff, upto=index)[index]
        commit.edits.pop(diff.path, None)
        if content is None:
            if state.version is not None:
                commit.edits[diff.path] = Edit(state.basis, None)
        # Even an edit that changes nothing resolves a conflict.
        elif content != state.basis or state.conflict or state.version is None:
            commit.edits[diff.path] = Edit(state.basis, content)

    def add_unchanged(self, path: str) -> FileDiff:
        """The file at path (from the top), added to the files as one the
        range doesn't change, so commits can edit it, if it isn't one of them
        already (by either path, if it's renamed)."""
        for diff in self.files:
            if path in (diff.path, diff.old_path):
                return diff
        out = self.git.run_bytes("ls-tree", "-z", "--full-tree", self.base, "--", path)
        entry, _, found = out.rstrip(b"\0").partition(b"\t")
        if found and found.decode(errors="surrogateescape") != path:
            raise RecommitError(f"{path} isn't a file")
        diff = FileDiff(path=path, status="=", old_mode=None, new_mode=None, old_blob=None, new_blob=None, binary=False)
        if found:
            mode, kind, blob = entry.decode().split(" ")
            content = self.git.blob(blob) if kind == "blob" else b""
            if kind != "blob" or mode in SPECIAL_MODES or b"\0" in content[:8000]:
                raise RecommitError(f"{path} isn't a text file, so it can't be edited")
            diff.old_mode = diff.new_mode = mode
            diff.old_blob = diff.new_blob = blob
            diff.old_lines = split_lines(content)
        self.files = sorted([*self.files, diff], key=lambda d: os.fsencode(d.path))
        self.unchanged.add(path)
        return diff

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
        return [diff.path for diff in self.files if not self._ends_as_tip(self.states(diff)[-1], diff)]

    @staticmethod
    def _ends_as_tip(state: FileState, diff: FileDiff) -> bool:
        """Whether the file in a commit is as it is at the tip (in the same
        place)."""
        everything = set(diff.ids)
        return state.version == pure_version(diff, everything) and state.path == diff.path_with(everything)

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
            previous = {diff.path: (diff.path_with(set()), pure_version(diff, set())) for diff in self.files}
            for position, commit in enumerate(self.plan.commits):
                if changes := self._index_changes(states, previous, position):
                    info = b"".join(entry(path, version) for path, version in changes)
                    self.git.run_bytes("update-index", "-z", "--index-info", input=info, env=env)
                tree = self.git.run("write-tree", env=env)
                message = self._message(position + 1, commit)
                parents = [] if parent is None else ["-p", parent]
                parent = self.git.run(
                    "commit-tree", tree, *parents, *sign, "-F", "-", input=message, env=_author_env(commit) | env
                )
                shas.append(parent)
        return shas

    def _index_changes(
        self,
        states: dict[str, list[FileState]],
        previous: dict[str, tuple[str, Version | None]],
        position: int,
    ) -> list[tuple[str, Version | None]]:
        """What changes in the index from the last commit to the one at
        position, as (path, version) (None removes the path), updating
        previous, each file's (path, version) so far. A file that moves (by
        its rename) leaves where it was first; no other file can be there,
        since git only finds renames to paths that weren't there, from paths
        that end up gone."""
        removals: list[tuple[str, Version | None]] = []
        updates: list[tuple[str, Version | None]] = []
        for diff in self.files:
            state = states[diff.path][position]
            old_path, old_version = previous[diff.path]
            if (state.path, state.version) == (old_path, old_version):
                continue
            if old_version is not None and (state.path != old_path or state.version is None):
                removals.append((old_path, None))
            if state.version is not None:
                updates.append((state.path, state.version))
            previous[diff.path] = (state.path, state.version)
        return removals + updates

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
        new_tip = self._replay_later(new_tip)
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

    def _replay_later(self, onto: str) -> str:
        """Replay the commits after the range onto the new commits, and return
        the last. Each keeps its tree, message and author, so nothing can
        conflict: writing.blocker makes sure the new commits end with the
        range's tree."""
        sign = ["-S"] if self.git.run("config", "--type=bool", "commit.gpgSign", check=False) == "true" else []
        for sha in self.later():
            message = self.git.run_bytes("cat-file", "commit", sha).partition(b"\n\n")[2]
            name, email, date = self.git.run("log", "-1", "--format=%an%x00%ae%x00%ad", "--date=raw", sha).split("\0")
            env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date}
            tree = self.git.run("rev-parse", f"{sha}^{{tree}}")
            onto = self.git.run_bytes("commit-tree", tree, "-p", onto, *sign, input=message, env=env).decode().strip()
        return onto

    def _only_in_tip(self, diff: FileDiff) -> bool:
        """Whether the file is at the tip but not in the last new commit, or
        not where it is at the tip, since its rename is left out (and wasn't
        untracked to start with, which it then goes back to being)."""
        if diff.path in self._untracked:
            return False
        if self.plan.commits:
            final = self.states(diff)[-1]
            version, path = final.version, final.path
        else:
            version, path = pure_version(diff, set()), diff.path_with(set())
        return (version is None or path != diff.path) and pure_version(diff, set(diff.ids)) is not None

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
