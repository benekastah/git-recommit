import os
import subprocess
from collections.abc import Mapping

from git_recommit.errors import RecommitError


class GitClient:
    """Runs git in directory, as `git -C <directory>` does."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        self.directory = os.fspath(directory)

    def _command(self, *args: str) -> list[str]:
        return ["git", "-C", self.directory, *args]

    def _env(self, env: Mapping[str, str] | None) -> dict[str, str] | None:
        return None if env is None else {**os.environ, **env}

    def run(
        self, *args: str, check: bool = True, input: str | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        """Run git and return stripped stdout."""
        result = subprocess.run(
            self._command(*args), capture_output=True, text=True, input=input, env=self._env(env), check=False
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
        return subprocess.run(self._command(*args), capture_output=True, check=False).returncode == 0

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
        return self.run_bytes("cat-file", "blob", sha)

    def hash_blob(self, content: bytes) -> str:
        """Write content to the object database, and return its id."""
        return self.run_bytes("hash-object", "-w", "--stdin", input=content).decode().strip()

    def merge_file(self, basis: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, bool]:
        """Merge the changes from basis to ours and from basis to theirs, as
        `git merge-file` does, and return the result and whether it conflicted
        (in which case it has conflict markers)."""
        ids = [self.hash_blob(content) for content in (ours, basis, theirs)]
        labels = ["-L", "edit", "-L", "before", "-L", "assigned"]
        result = subprocess.run(
            self._command("merge-file", "-p", "--object-id", *labels, *ids), capture_output=True, check=False
        )
        # The exit status is the number of conflicts, or negative (shown as
        # 128 or more) on an error.
        if not 0 <= result.returncode < 128:
            raise RecommitError(f"git merge-file failed:\n{result.stderr.decode(errors='replace').strip()}")
        return result.stdout, result.returncode > 0
