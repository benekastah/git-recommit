import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from typing import TypeAlias

from git_recommit.errors import RecommitError

# A command and an environment for git: on Windows, as str, as the OS keeps
# them; elsewhere, as bytes (see GitClient._command and _env).
if sys.platform == "win32":
    Command: TypeAlias = list[str]
    Env: TypeAlias = Mapping[str, str]
else:
    Command: TypeAlias = list[bytes]
    Env: TypeAlias = Mapping[str, str] | Mapping[bytes, bytes]

# What git stores as text (UTF-8), rather than as a path in the filesystem's
# encoding, of what git recommit puts in git's environment.
IDENTITY = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"}


# Whether git's processes are started with close_fds. Not on Linux or macOS:
# without it, Python can start them with posix_spawn, which on macOS is about
# a third faster, and its own file descriptors aren't inherited anyway (PEP
# 446). On Windows, yes: without it, a process one thread starts could
# inherit (and hold open) the pipes of one another thread is starting.
CLOSE_FDS = sys.platform == "win32"


@functools.cache
def git_executable() -> str:
    """The git that git recommit runs: $GIT_RECOMMIT_GIT, or else the git on
    PATH, whatever it is (e.g. a wrapper), found once, by its full path
    (which posix_spawn needs)."""
    if configured := os.environ.get("GIT_RECOMMIT_GIT"):
        return configured
    found = shutil.which("git")
    return os.path.abspath(found) if found else "git"


def encode(text: str) -> bytes:
    """What git gave as text (e.g. a path or a message, decoded as UTF-8 with
    surrogateescape, as GitClient.run does), as git gave it."""
    return text.encode("utf-8", errors="surrogateescape")


def fs_path(path: str) -> str | bytes:
    """A path in the filesystem (e.g. a temporary file, not a path git gave),
    as an argument for GitClient: in the filesystem's encoding."""
    return path if sys.platform == "win32" else os.fsencode(path)


def _describe(args: tuple[str | bytes, ...]) -> str:
    return " ".join(map(os.fsdecode, args))


# A full object id: SHA-1 or SHA-256.
OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class GitClient:
    """Runs git in directory, as `git -C <directory>` does."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        self.directory = os.fspath(directory)
        # `git cat-file --batch-command`, started the first time an object is
        # read or a name resolved, and what keeps threads from talking to it at
        # once.
        self._cat_file: subprocess.Popen[bytes] | None = None
        self._cat_file_lock = threading.Lock()
        # Where git keeps each file asked about (see git_paths), and the path
        # from the top of the worktree down to directory, which don't change.
        self._git_paths: dict[str, str] = {}
        self._prefix: str | None = None

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        """Stop the cat-file process, if one was started."""
        if self._cat_file is not None:
            if self._cat_file.stdin is not None:
                try:
                    self._cat_file.stdin.close()
                except OSError:
                    # It had stopped already (on Windows, maybe not as a
                    # BrokenPipeError).
                    pass
            self._cat_file.wait()
            self._cat_file = None

    def _command(self, *args: str | bytes) -> Command:
        """git's command line. Bytes are paths in the filesystem, from fs_path
        (on Windows, they're str)."""
        if sys.platform == "win32":
            return [git_executable(), "-C", self.directory, *map(os.fsdecode, args)]
        # The arguments (e.g. paths git gave) as git gave them, whatever the
        # locale's encoding (which, e.g. with LC_ALL=C, can't hold a name that
        # isn't ASCII); the directory, a path in the filesystem's encoding.
        return [
            os.fsencode(git_executable()),
            b"-C",
            os.fsencode(self.directory),
            *(a if isinstance(a, bytes) else encode(a) for a in args),
        ]

    def _env(self, env: Mapping[str, str] | None = None) -> Env:
        # Not GIT_LITERAL_PATHSPECS, which the user's hooks would inherit: a
        # command given paths git reported marks them literal (see literal).
        if sys.platform == "win32" or not env:
            return {**os.environ, **(env or {})}
        # An author as git gave it (or the user typed it), whatever the
        # locale's encoding; other values are paths (e.g. GIT_INDEX_FILE), in
        # the filesystem's encoding.
        added = {key.encode(): encode(value) if key in IDENTITY else os.fsencode(value) for key, value in env.items()}
        return {**os.environb, **added}

    def process(
        self,
        *args: str | bytes,
        capture_output: bool = False,
        input: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run git and return the process when it's done, whatever its exit
        status; every git process but cat-file's is run by this."""
        return subprocess.run(
            self._command(*args),
            capture_output=capture_output,
            input=input,
            env=self._env(env),
            close_fds=CLOSE_FDS,
            check=False,
        )

    def run(
        self, *args: str | bytes, check: bool = True, input: str | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        """Run git and return stripped stdout."""
        # UTF-8, as git writes (not the locale's encoding, e.g. cp1252 on
        # Windows), keeping what isn't as surrogates, as paths are; and as
        # bytes, so line endings stay as they are (text mode would write \r\n
        # for each \n on Windows).
        result = self.process(
            *args,
            capture_output=True,
            input=None if input is None else input.encode("utf-8", errors="surrogateescape"),
            env=env,
        )
        if check and result.returncode != 0:
            raise RecommitError(f"git {_describe(args)} failed:\n{result.stderr.decode(errors='replace').strip()}")
        return result.stdout.decode("utf-8", errors="surrogateescape").strip()

    def run_bytes(self, *args: str | bytes, input: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
        """Run git and return stdout as it is, for file contents and patches."""
        result = self.process(*args, capture_output=True, input=input, env=env)
        if result.returncode != 0:
            raise RecommitError(f"git {_describe(args)} failed:\n{result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def ok(self, *args: str | bytes) -> bool:
        """Run git and report whether it succeeded."""
        return self.process(*args, capture_output=True).returncode == 0

    def passthrough(self, *args: str | bytes, env: Mapping[str, str] | None = None) -> bool:
        """Run git with its output going straight to the terminal."""
        return self.process(*args, env=env).returncode == 0

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
        """The commit rev names, or None if it doesn't name one."""
        try:
            return self._resolve(f"{rev}^{{commit}}")
        except RecommitError:
            # git cat-file exits for some names (e.g. @{u} without an
            # upstream), rather than saying they name nothing.
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
        tree = self._resolve(f"{sha}^{{tree}}")
        if tree is None:
            raise RecommitError(f"{sha} isn't a commit or tree")
        return tree

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def blob(self, sha: str) -> bytes:
        """A blob's content."""
        return self.read_object(sha, "blob")

    def read_object(self, sha: str, kind: str) -> bytes:
        """An object's content (as `git cat-file <kind> <sha>`, e.g. a
        commit's, its headers and message, exactly as they are)."""
        if not OBJECT_ID.fullmatch(sha):
            # Anything else (e.g. a line ending) would put the process out of
            # step with what's read back.
            raise RecommitError(f"{sha!r} isn't an object id")
        with self._cat_file_lock:
            header = self._cat_file_command(b"contents " + sha.encode())
            if len(header) != 3:
                raise RecommitError(f"{sha} isn't a {kind}")
            assert self._cat_file is not None and self._cat_file.stdout is not None
            # Read even if it's another kind, so the process stays in step.
            content = self._cat_file.stdout.read(int(header[2]))
            self._cat_file.stdout.read(1)
        if header[1] != kind.encode():
            raise RecommitError(f"{sha} isn't a {kind}")
        return content

    def _resolve(self, name: str) -> str | None:
        """The object name names (as rev-parse --verify would find it), or
        None if it names none, without starting a process."""
        if not name or "\n" in name:
            # A line ending would put the process out of step.
            return None
        with self._cat_file_lock:
            header = self._cat_file_command(b"info " + encode(name))
        # "<name> missing" can split in three too, if name has a space.
        found = len(header) == 3 and OBJECT_ID.fullmatch(header[0].decode()) and header[2].isdigit()
        return header[0].decode() if found else None

    def _cat_file_command(self, command: bytes) -> list[bytes]:
        """Send command to one `git cat-file --batch-command` process, kept for
        reading every object and resolving every name, rather than a process
        each; and return the reply's first line: "<sha> <type> <size>" (for
        contents, then the content and a newline), or "<name> missing" (or
        ambiguous), split. It sees refs and objects as they are when asked,
        even when other processes have changed them since it started."""
        if self._cat_file is not None and self._cat_file.poll() is not None:
            # It stopped since the last command (e.g. killed while git
            # recommit was suspended): start another, rather than take this
            # name for one that names nothing.
            self.close()
        if self._cat_file is None:
            self._cat_file = subprocess.Popen(
                self._command("cat-file", "--batch-command"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Which has warnings even for names it resolves (e.g. a ref
                # name that's both a branch and a tag), as rev-parse --quiet
                # doesn't; errors are for resolve_commit to say.
                stderr=subprocess.DEVNULL,
                env=self._env(),
                close_fds=CLOSE_FDS,
            )
        assert self._cat_file.stdin is not None and self._cat_file.stdout is not None
        try:
            self._cat_file.stdin.write(command + b"\n")
            self._cat_file.stdin.flush()
            reply = self._cat_file.stdout.readline()
        except OSError:
            # It had stopped (see close).
            reply = b""
        if not reply:
            # It stopped; the next command starts another.
            self.close()
            raise RecommitError(f"git cat-file stopped at {command.decode(errors='replace')!r}")
        return reply.split()

    def clean(self, path: str, content: bytes) -> bytes:
        """content as `git add` would commit it at path: through the clean
        filters and line-ending conversion its attributes and config ask for
        (e.g. CRLF to LF with core.autocrlf)."""
        return self.blob(self.run_bytes("hash-object", "-w", "--path", path, "--stdin", input=content).decode().strip())

    def hash_blob(self, content: bytes) -> str:
        """Write content to the object database, and return its id."""
        return self.hash_blobs([content])[0]

    def hash_blobs(self, contents: list[bytes]) -> list[str]:
        """Write each content to the object database with one git process,
        and return their ids."""
        if not contents:
            return []
        with tempfile.TemporaryDirectory() as scratch:
            paths = []
            for i, content in enumerate(contents):
                paths.append(os.path.join(scratch, str(i)))
                with open(paths[-1], "wb") as f:
                    f.write(content)
            # --no-filters: the contents are what to commit, as they are.
            # (Paths in the filesystem, so in its encoding.)
            stdin = b"".join(os.fsencode(path) + b"\n" for path in paths)
            out = self.run_bytes("hash-object", "-w", "--no-filters", "--stdin-paths", input=stdin)
        return out.decode().split()

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
            result = self.process("merge-file", "-p", *labels, *map(fs_path, paths), capture_output=True)
        # The exit status is the number of conflicts, or negative (shown as
        # 128 or more) on an error.
        if not 0 <= result.returncode < 128:
            raise RecommitError(f"git merge-file failed:\n{result.stderr.decode(errors='replace').strip()}")
        return result.stdout, result.returncode > 0
