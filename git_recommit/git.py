import os
import subprocess
import tempfile
import threading
from collections.abc import Mapping

from git_recommit.errors import RecommitError


class GitClient:
    """Runs git in directory, as `git -C <directory>` does."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        self.directory = os.fspath(directory)
        # `git cat-file --batch`, started the first time a blob is read, and
        # what keeps threads from talking to it at once.
        self._cat_file: subprocess.Popen[bytes] | None = None
        self._cat_file_lock = threading.Lock()

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        """Stop the cat-file process, if one was started."""
        if self._cat_file is not None:
            if self._cat_file.stdin is not None:
                self._cat_file.stdin.close()
            self._cat_file.wait()
            self._cat_file = None

    def _command(self, *args: str) -> list[str]:
        return ["git", "-C", self.directory, *args]

    def _env(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        # Not GIT_LITERAL_PATHSPECS, which the user's hooks would inherit: a
        # command given paths git reported marks them literal (see literal).
        return {**os.environ, **(env or {})}

    def run(
        self, *args: str, check: bool = True, input: str | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        """Run git and return stripped stdout."""
        # UTF-8, as git writes (not the locale's encoding, e.g. cp1252 on
        # Windows), keeping what isn't as surrogates, as paths are.
        result = subprocess.run(
            self._command(*args),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            input=input,
            env=self._env(env),
            check=False,
        )
        if check and result.returncode != 0:
            raise RecommitError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def run_bytes(self, *args: str, input: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
        """Run git and return stdout as it is, for file contents and patches."""
        result = subprocess.run(self._command(*args), capture_output=True, input=input, env=self._env(env), check=False)
        if result.returncode != 0:
            raise RecommitError(f"git {' '.join(args)} failed:\n{result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def ok(self, *args: str) -> bool:
        """Run git and report whether it succeeded."""
        return subprocess.run(self._command(*args), capture_output=True, env=self._env(), check=False).returncode == 0

    def passthrough(self, *args: str, env: Mapping[str, str] | None = None) -> bool:
        """Run git with its output going straight to the terminal."""
        return subprocess.run(self._command(*args), env=self._env(env), check=False).returncode == 0

    def commit_sha(self, rev: str) -> str | None:
        """The commit rev names, or None if it doesn't name one."""
        return (
            self.run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}", check=False) or None
        )

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def blob(self, sha: str) -> bytes:
        """A blob's content, read with one `git cat-file --batch` process kept
        for all of them, rather than a process each."""
        with self._cat_file_lock:
            return self._read_blob(sha)

    def _read_blob(self, sha: str) -> bytes:
        if self._cat_file is None:
            self._cat_file = subprocess.Popen(
                self._command("cat-file", "--batch"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self._env()
            )
        assert self._cat_file.stdin is not None and self._cat_file.stdout is not None
        self._cat_file.stdin.write(sha.encode() + b"\n")
        self._cat_file.stdin.flush()
        # "<sha> <type> <size>", then the content and a newline; or "<sha> missing".
        header = self._cat_file.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise RecommitError(f"{sha} isn't a blob")
        content = self._cat_file.stdout.read(int(header[2]))
        self._cat_file.stdout.read(1)
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
            out = self.run("hash-object", "-w", "--no-filters", "--stdin-paths", input="\n".join(paths) + "\n")
        return out.split()

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
            result = subprocess.run(
                self._command("merge-file", "-p", *labels, *paths), capture_output=True, env=self._env(), check=False
            )
        # The exit status is the number of conflicts, or negative (shown as
        # 128 or more) on an error.
        if not 0 <= result.returncode < 128:
            raise RecommitError(f"git merge-file failed:\n{result.stderr.decode(errors='replace').strip()}")
        return result.stdout, result.returncode > 0
