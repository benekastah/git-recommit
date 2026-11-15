"""A recommit: the commits being rebuilt, the plan for the new ones, and
building them."""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from git_recommit import diff as diffs
from git_recommit.diff import FileDiff, numbered
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
from git_recommit.plan import Author, Edit, NewCommit, Plan
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


def worktree_commit(git: GitClient, head: str) -> str | None:
    """A commit, on head, of the working tree's tracked files (and whatever is
    staged), as `git stash` makes; or None if they have no uncommitted changes.

    It isn't on any branch; recommit uses it as the end of the diff, so that
    uncommitted changes can be assigned to new commits like committed ones.
    """
    # --no-optional-locks: don't refresh (and so write) the user's index.
    if not git.run("--no-optional-locks", "status", "--porcelain", "--untracked-files=no"):
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


# A line of `git blame --porcelain` that starts a line's entry: the commit,
# then the line's number in the commit, then its number in the file blamed.
BLAME_ENTRY = re.compile(rb"^([0-9a-f]{40,64}) \d+ (\d+)")


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
        self._conflicts: tuple[tuple[object, ...], list[Conflict]] | None = None

    @classmethod
    def load(cls, git: GitClient, spec: str) -> "Session":
        check_nothing_in_progress(git)
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
            check_nothing_in_progress(self.git)
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

    def head_moved(self) -> bool:
        """Whether HEAD has moved since the diff was loaded (e.g. the user
        committed while git recommit was suspended)."""
        return self.git.commit_sha("HEAD") != self.head

    def clear(self) -> None:
        """Start from scratch, with no commits."""
        self.plan = Plan()

    def restore_old_commits(self) -> None:
        """Start from the commits being rebuilt: one new commit for each, with
        its message and author, taking the changes it made (by git blame).

        Where a commit's changes overlap a later one's (e.g. it adds a line a
        later commit rewrites), the changes alone can't make the commit as it
        was, so it gets edits that do. Building the plan unchanged makes the
        same trees, but for files that end up as they started (see ISSUES.md).
        """
        shas = self.git.run("rev-list", "--reverse", f"{self.base}..{self.head}").split()
        self.plan = plan = Plan()
        keys = {}
        for sha in shas:
            # Exactly as it is in the commit (git log would trim it), for
            # writing it unchanged; in the plan, without its final newline,
            # as messages the user writes are.
            exact = self.git.run_bytes("cat-file", "commit", sha).partition(b"\n\n")[2].decode(errors="surrogateescape")
            name, email, date = self.git.run("log", "-1", "--format=%an%x00%ae%x00%ad", "--date=raw", sha).split("\0")
            commit = plan.add(exact.removesuffix("\n"), author=Author(name, email, date))
            commit.old_message = exact
            keys[sha] = commit.key
        # A line last there in a commit (or base) was removed by the next one.
        removed_by = dict(zip([self.base, *shas], shas, strict=False))
        touched_by = self._touched_by(shas)
        uncommitted = set(self._changed_paths(self.head, self.tip)) if self.uncommitted else set()
        for diff in self.files:
            # The last commit to change the file takes what blame can't say
            # who made, as well as what blame doesn't cover.
            touched = touched_by.get(diff.path, [])
            last = keys.get(touched[-1]) if touched else None
            if diff.meta_id is not None:
                plan.assign([diff.meta_id], last)
            if diff.whole:
                continue
            if len(touched) == 1 and diff.path not in uncommitted:
                # Only one commit changed it, so it made every change.
                plan.assign([i for i in diff.ids if i != diff.meta_id], last)
                continue
            for i, made_by in self._made_by(diff, removed_by).items():
                plan.assign([i], keys.get(made_by or "", last))
        self._reproduce(shas)

    def _changed_paths(self, old: str, new: str) -> list[str]:
        out = self.git.run_bytes("diff-tree", "-r", "--no-renames", "--name-only", "-z", old, new)
        return [path.decode(errors="surrogateescape") for path in out.split(b"\0") if path]

    def _touched_by(self, shas: list[str]) -> dict[str, list[str]]:
        """For each path, the commits of shas (in order) that changed it."""
        touched: dict[str, list[str]] = {}
        for parent, sha in zip([self.base, *shas], shas, strict=False):
            for path in self._changed_paths(parent, sha):
                touched.setdefault(path, []).append(sha)
        return touched

    def _made_by(self, diff: FileDiff, removed_by: dict[str, str]) -> dict[int, str | None]:
        """For each of a text file's changes that was made before HEAD, the
        old commit that made it, by git blame, or None where blame and the
        diff disagree about the line (e.g. a blank line that moved)."""
        added = self._blame(diff.path, reverse=False) if diff.new_blob else {}
        removed = self._blame(diff.path, reverse=True) if diff.old_blob else {}
        made_by: dict[int, str | None] = {}
        for hunk in diff.hunks:
            for line, old, new in numbered(hunk):
                if line.id is None:
                    continue
                if line.kind == "+":
                    # Added by an old commit, or after HEAD.
                    by = added.get(new)
                    if by == self.tip and self.uncommitted:
                        continue
                    made_by[line.id] = by
                else:
                    # Last there in base or an old commit, or in HEAD.
                    there = removed.get(old)
                    if there == self.head and self.uncommitted:
                        continue
                    made_by[line.id] = removed_by.get(there or "")
        return made_by

    def _blame(self, path: str, reverse: bool) -> dict[int | None, str]:
        """For each line of the file (at tip, or with reverse, at base), the
        commit that last added it (or with reverse, the last that had it)."""
        args = ["blame", "--porcelain", *(["--reverse"] if reverse else []), f"{self.base}..{self.tip}"]
        lines: dict[int | None, str] = {}
        for entry in self.git.run_bytes(*args, "--", path).split(b"\n"):
            if match := BLAME_ENTRY.match(entry):
                lines[int(match[2])] = match[1].decode()
        return lines

    def _reproduce(self, shas: list[str]) -> None:
        """Give each commit of the plan the edits it needs to have each text
        file as old commit shas[i] does."""
        object_format = self.git.run("rev-parse", "--show-object-format")
        trees = [self._blobs(sha) for sha in shas]
        for diff in self.files:
            # An edit can't change a binary file.
            if diff.whole:
                continue
            for index, (commit, blobs) in enumerate(zip(self.plan.commits, trees, strict=True)):
                actual = blobs.get(diff.path)
                # Nor delete a file.
                if actual is None:
                    continue
                # Only up to this commit: an edit here changes the later ones.
                version = self.states(diff, upto=index)[index].version
                content = None if version is None else version.content
                if content is not None and blob_id(content, object_format) == actual:
                    continue
                self.set_edit(commit.key, diff, self.git.blob(actual))

    def _blobs(self, sha: str) -> dict[str, str]:
        """Each file's blob in the commit."""
        blobs = {}
        for entry in self.git.run_bytes("ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0"):
            if entry:
                meta, _, path = entry.partition(b"\t")
                _, kind, blob = meta.decode().split(" ")
                if kind == "blob":
                    blobs[path.decode(errors="surrogateescape")] = blob
        return blobs

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
                    self.git.run_bytes("update-index", "-z", "--index-info", input=b"".join(updates), env=env)
                tree = self.git.run("write-tree", env=env)
                # An old commit's message is kept exactly; a new or changed one
                # is ended with a newline, and given to the commit-msg hook.
                if commit.old_message is not None and commit.message == commit.old_message.removesuffix("\n"):
                    message = commit.old_message
                else:
                    message = commit.message if commit.message.endswith("\n") else commit.message + "\n"
                    message = self._commit_msg_hook(index + 1, commit, message)
                author = commit.author
                author_env = (
                    {}
                    if author is None
                    else {
                        "GIT_AUTHOR_NAME": author.name,
                        "GIT_AUTHOR_EMAIL": author.email,
                        "GIT_AUTHOR_DATE": author.date,
                    }
                )
                parent = self.git.run(
                    "commit-tree", tree, "-p", parent, *sign, "-F", "-", input=message, env=author_env | env
                )
                shas.append(parent)
        return shas

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
        """Whether the file is at the tip but not in the last new commit."""
        final = self.states(diff)[-1].version if self.plan.commits else pure_version(diff, set())
        return final is None and pure_version(diff, set(diff.ids)) is not None


def literal(path: str) -> str:
    """A pathspec for exactly the file at path (a path git reported, from the
    top of the worktree), not a pattern: a[1].txt mustn't match a1.txt."""
    return f":(top,literal){path}"


def blob_id(content: bytes, object_format: str) -> str:
    """The id git gives a blob of content, without asking it."""
    return hashlib.new(object_format, b"blob %d\0" % len(content) + content).hexdigest()
