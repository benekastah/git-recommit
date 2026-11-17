"""Running git, through py-git-client (`git_client`), which starts every git
process the quickest way each system allows and reads objects through one
`git cat-file --batch-command`. This adds what git recommit needs on top:
its errors as RecommitErrors, the user's locale for git, and the reads it
makes (commits, trees and blobs, paths in the git directory, merges)."""

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Generator, Mapping

from git_client import CLIENT_TIMEOUT, CatFile, EnvValue, Git, GitError, Timeout, UnknownRevisionError, decode, encode
from git_client import hash_blobs as _hash_blobs

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
    GitErrors; ok and passthrough say whether git succeeded (a timeout still
    raises GitTimeoutError). What it has of Git's as it is (var, resolve, object_id_length) raises GitErrors."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        try:
            super().__init__(directory, env_var="GIT_RECOMMIT_GIT")
        except (FileNotFoundError, ValueError) as e:
            # find_git's: no git on PATH, or on Windows, a batch file.
            raise RecommitError(str(e)) from e
        # For reading every object and resolving every name, rather than a
        # process each; started the first time it's needed. Through a Git of
        # its own, not this client, which would make a reference cycle, so
        # __del__ would only stop it when the cycle collector ran. (It runs
        # nothing that inherits the C locale that Git gives it.)
        self.cat_file = CatFile(Git(self.directory, program=self.program))
        # Where git keeps each file asked about (see git_paths), and the path
        # from the top of the worktree down to directory, which don't change.
        self._git_paths: dict[str, str] = {}
        self._prefix: str | None = None

    def __del__(self) -> None:
        # Not if __init__ failed before making it.
        if hasattr(self, "cat_file"):
            self.close()

    def close(self) -> None:
        """Stop the cat-file process, if one was started."""
        self.cat_file.close()

    def environment(self, env: Mapping[str, EnvValue] | None = None, *, english: bool = False) -> dict[str, str]:
        """As Git's, but always in the user's locale, never with the LC_ALL=C
        Git gives a command whose output it reads: the hooks, filters and
        signing programs git runs for git recommit (e.g. commit-msg) would
        inherit it, and git's messages are for the user to read."""
        del english
        return super().environment(env)

    def run(
        self,
        *args: str | bytes,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> str:
        """Run git and return its stdout, stripped; unless check is False,
        raise a RecommitError if it fails."""
        if check:
            # Git.run calls run_bytes, whose reported() does this too; here
            # as well, in case py-git-client's Git.run stops calling it.
            with reported():
                return super().run(*args, input=input, env=env, timeout=timeout)
        encoded = None if input is None else encode(input)
        result = self.process(*args, capture_output=True, input=encoded, env=env, timeout=timeout)
        return decode(result.stdout).strip()

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
            args = [a for name in new for a in ("--git-path", name)]
            out = self.run_bytes("rev-parse", "--path-format=absolute", *args)
            self._git_paths.update(zip(new, map(os.fsdecode, out.splitlines()), strict=True))
        return [self._git_paths[name] for name in names]

    def prefix(self) -> str:
        """The path from the top of the worktree down to directory, ending in
        a slash (or empty, at the top or outside the worktree, where git takes
        paths from the top), to put before a path from directory."""
        if self._prefix is None:
            self._prefix = self.run("rev-parse", "--show-prefix")
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
        if (sha := self.commit_sha(rev)) is not None:
            return sha
        # Only now, for the reason, which git cat-file doesn't give.
        result = self.process("rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}", capture_output=True)
        # But for what git says for any name that isn't a commit.
        lines = result.stderr.decode(errors="replace").splitlines()
        reason = "\n".join(line for line in lines if line != "fatal: Needed a single revision")
        raise RecommitError(f"{rev} isn't a commit" + (f":\n{reason}" if reason else ""))

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

    def ok(
        self, *args: str | bytes, env: Mapping[str, EnvValue] | None = None, timeout: Timeout = CLIENT_TIMEOUT
    ) -> bool:
        """Whether git succeeded: False for any exit status but 0, not only
        git's no (1), unlike Git's, which raises for the rest. A timeout
        still raises GitTimeoutError."""
        result = self.process(*args, capture_output=True, stdout=subprocess.DEVNULL, env=env, timeout=timeout)
        return result.returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether ancestor is in descendant's history (or is descendant);
        False too if git can't say (e.g. one names nothing)."""
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def blob(self, sha: str) -> bytes:
        """A blob's content."""
        return self.read_object(sha, "blob")

    def read_object(self, sha: str, kind: str) -> bytes:
        """An object's content (as `git cat-file <kind> <sha>`, e.g. a
        commit's, its headers and message, exactly as they are)."""
        with reported():
            try:
                found, content = self.cat_file.read(sha)
            except ValueError as e:
                raise RecommitError(f"{sha!r} isn't an object id") from e
            except UnknownRevisionError as e:
                raise RecommitError(f"{sha} isn't a {kind}") from e
        if found != kind:
            raise RecommitError(f"{sha} isn't a {kind}")
        return content

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
            return _hash_blobs(self, contents)

    def merge_file(self, basis: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
        """Merge the changes from basis to ours and from basis to theirs, as
        `git merge-file` does, and return the result and whether it conflicted
        (in which case it has conflict markers). Nothing is written to the
        repository."""
        labels = ["-L", "edit", "-L", "before", "-L", "assigned"]
        with tempfile.TemporaryDirectory() as scratch:
            paths = [os.path.join(scratch, name) for name in ("ours", "basis", "theirs")]
            for path, content in zip(paths, (ours, basis, theirs), strict=True):
                with open(path, "wb") as f:
                    f.write(content)
            # Paths in the filesystem, so in its encoding.
            result = self.process("merge-file", "-p", *labels, *map(os.fsencode, paths), capture_output=True)
        # The exit status is the number of conflicts, or negative (shown as
        # 128 or more) on an error.
        if not 0 <= result.returncode < 128:
            raise RecommitError(f"git merge-file failed:\n{result.stderr.decode(errors='replace').strip()}")
        return result.stdout, result.returncode > 0
