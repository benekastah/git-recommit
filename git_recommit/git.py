"""Running git, through py-git-client (`git_client`), which starts every git
process the quickest way each system allows and reads objects through one
`git cat-file --batch-command`. This adds what git recommit needs on top:
its errors as RecommitErrors, and the reads it makes (commits, trees and
blobs, paths in the git directory, merges)."""

import contextlib
import os
from collections.abc import Generator, Mapping

from git_client import (
    CLIENT_TIMEOUT,
    CatFile,
    EnvValue,
    Git,
    GitError,
    Timeout,
    decode,
    encode,
    git_paths,
    hash_blobs,
)
from git_client import merge_file as _merge_file
from git_client.cat_file import ObjectType

from git_recommit.errors import RecommitError


@contextlib.contextmanager
def reported() -> Generator[None]:
    """Turn a GitError in the block into a RecommitError saying what git
    said, to report to the user without a traceback."""
    try:
        yield
    except GitError as e:
        raise RecommitError(f"git {' '.join(e.command)} failed:\n{e.stderr.strip()}") from e


class GitClient(Git):
    """Runs git in directory, as `git -C <directory>` does: the git
    $GIT_RECOMMIT_GIT names, or else the first git on PATH (which, as `git
    recommit` runs it, is the git that did: git puts its exec-path first),
    by its full path (a RecommitError if there's none).

    run, run_bytes and the reads it adds raise RecommitErrors, not
    GitErrors. What it has of Git's as it is (e.g. var, resolve, succeeds,
    passthrough) raises GitErrors."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        try:
            super().__init__(directory, env_var="GIT_RECOMMIT_GIT")
        except (FileNotFoundError, ValueError) as e:
            # find_git's: no git on PATH, or on Windows, a batch file.
            raise RecommitError(str(e)) from e
        # For reading every object and resolving every name, rather than a
        # process each; started the first time it's needed. Weakly, so that
        # it stops as soon as this client is no longer used, not when the
        # cycle collector runs.
        self.cat_file = CatFile(self, weak=True)
        # Where git keeps each file asked about (see git_paths), and the path
        # from the top of the worktree down to directory, which don't change.
        self._git_paths: dict[str, str] = {}
        self._prefix: str | None = None

    def run(
        self,
        *args: str | bytes,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
        strip: bool = True,
    ) -> str:
        """Run git and return its stdout, stripped unless strip is False;
        unless check is False, raise a RecommitError if it fails."""
        if check:
            # Git.run calls run_bytes, whose reported() does this too; here
            # as well, in case py-git-client's Git.run stops calling it.
            with reported():
                return super().run(*args, input=input, env=env, timeout=timeout, strip=strip)
        encoded = None if input is None else encode(input)
        result = self.process(*args, capture_output=True, input=encoded, env=env, timeout=timeout)
        output = decode(result.stdout)
        return output.strip() if strip else output

    def run_bytes(
        self,
        *args: str | bytes,
        input: bytes | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> bytes:
        """Run git and return its stdout as it is, for file contents and
        patches; raise a RecommitError if it fails."""
        with reported():
            return super().run_bytes(*args, input=input, env=env, timeout=timeout)

    def git_paths(self, *names: str) -> list[str]:
        """Where each of names is in the git directory (as `git rev-parse
        --git-path` says), as paths in the filesystem: so in its encoding,
        not as text. Each is only asked of git once."""
        if new := [name for name in dict.fromkeys(names) if name not in self._git_paths]:
            with reported():
                found = git_paths(self, *new)
            self._git_paths.update(zip(new, map(os.fspath, found), strict=True))
        return [self._git_paths[name] for name in names]

    def prefix(self, *, timeout: Timeout = CLIENT_TIMEOUT) -> str:
        """As Git's (the path from the top of the worktree down to directory,
        to put before a path from directory), but only asked of git once."""
        if self._prefix is None:
            with reported():
                self._prefix = super().prefix(timeout=timeout)
        return self._prefix

    def cdup(self) -> str:
        """The path from directory up to the top of the worktree (as prefix,
        the other way), to put before a path from the top."""
        # Not --show-cdup, which outside the worktree gives its path.
        return "../" * self.prefix().count("/")

    def commit_sha(self, rev: str) -> str | None:
        """The commit rev names, or None if it doesn't name one (or git can't
        say: resolve_commit says why)."""
        try:
            return self.cat_file.resolve(f"{rev}^{{commit}}")
        except GitError:
            return None

    def resolve_commit(self, rev: str) -> str:
        """The commit rev names (e.g. one the user typed), or if it doesn't
        name one, an error that says why, as git does."""
        name = f"{rev}^{{commit}}"
        try:
            return self.cat_file.verify(name)
        except GitError as e:
            # git's reason, but for what cat-file says of any name that names
            # nothing.
            reason = "" if e.stderr == f"{name} missing" else e.stderr
            raise RecommitError(f"{rev} isn't a commit" + (f":\n{reason}" if reason else "")) from e

    def head(self) -> str | None:
        """The commit HEAD is at, or None if there's none yet."""
        return self.commit_sha("HEAD")

    def tree(self, sha: str) -> str:
        """The tree of the commit with id sha, without starting a process (or
        itself, if it's a tree)."""
        with reported():
            tree = self.cat_file.resolve(f"{sha}^{{tree}}")
        if tree is None:
            raise RecommitError(f"{sha} isn't a commit or tree")
        return tree

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether ancestor is in descendant's history (or is descendant);
        False too if git can't say (e.g. one names nothing)."""
        return self.succeeds("merge-base", "--is-ancestor", ancestor, descendant)

    def blob(self, sha: str) -> bytes:
        """A blob's content."""
        return self.read_object(sha, "blob")

    def read_object(self, sha: str, kind: ObjectType) -> bytes:
        """An object's content (as `git cat-file <kind> <sha>`, e.g. a
        commit's, its headers and message, exactly as they are)."""
        with reported():
            try:
                return self.cat_file.read(sha, kind)[1]
            except ValueError as e:
                raise RecommitError(f"{sha!r} isn't an object id") from e

    def clean(self, path: str, content: bytes) -> bytes:
        """content as `git add` would commit it at path: through the clean
        filters and line-ending conversion its attributes and config ask for
        (e.g. CRLF to LF with core.autocrlf)."""
        return self.blob(self.run_bytes("hash-object", "-w", "--path", path, "--stdin", input=content).decode().strip())

    def hash_blob(self, content: bytes) -> str:
        """Write content to the object database, and return its id."""
        return self.hash_blobs([content])[0]

    def hash_blobs(self, contents: list[bytes]) -> list[str]:
        """Write each content to the object database, as it is (no filters),
        with one git process, and return their ids."""
        with reported():
            return hash_blobs(self, contents)

    def merge_file(self, basis: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
        """Merge the changes from basis to ours and from basis to theirs, as
        `git merge-file` does, and return the result and whether it conflicted
        (in which case it has conflict markers). Nothing is written to the
        repository."""
        with reported():
            merged, conflicts = _merge_file(
                self, ours=ours, base=basis, theirs=theirs, labels=("edit", "before", "assigned")
            )
        return merged, conflicts > 0
