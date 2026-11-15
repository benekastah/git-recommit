import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

import pytest

from git_recommit.git import GitClient
from git_recommit.session import Session


@dataclass
class Repo:
    path: pathlib.Path

    @property
    def client(self) -> GitClient:
        return GitClient(self.path)

    def git(self, *args: str) -> str:
        # UTF-8, as git writes, whatever the locale's encoding.
        result = subprocess.run(["git", *args], cwd=self.path, capture_output=True, encoding="utf-8", check=False)
        if result.returncode != 0:
            pytest.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def write(self, name: str, content: str | bytes) -> None:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        path.write_bytes(content)

    def executable(self, name: str) -> None:
        """Make the file executable, as git sees it: in the index too, since
        on Windows, where a file has no executable bit, git keeps the mode
        there (core.fileMode is off)."""
        (self.path / name).chmod(0o755)
        self.git("add", "--", name)
        self.git("update-index", "--chmod=+x", "--", name)

    def commit(self, message: str, **files: str | bytes | None) -> str:
        """Write (or with None, delete) the files, and commit everything."""
        for name, content in files.items():
            if content is None:
                (self.path / name).unlink()
            else:
                self.write(name, content)
        self.git("add", "--all")
        # "[<branch> <id>] <subject>", with the whole id, rather than asking
        # for it after, with another git process.
        summary = self.git("-c", "core.abbrev=no", "commit", "--allow-empty", "-m", message).splitlines()[0]
        match = re.match(r"\[.* ([0-9a-f]{40}|[0-9a-f]{64})\] ", summary)
        assert match is not None, summary
        return match[1]

    def show(self, rev: str, name: str) -> str | None:
        """The file in rev, or None if it isn't there."""
        result = subprocess.run(["git", "show", f"{rev}:{name}"], cwd=self.path, capture_output=True, check=False)
        return result.stdout.decode() if result.returncode == 0 else None

    def session(self, spec: str) -> Session:
        return Session.load(self.client, spec)

    def recommit(
        self,
        *args: str,
        expect_success: bool = True,
        input: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run `git recommit <args>` as a separate process (without a terminal),
        with input on its stdin (as UTF-8, with its line endings as they are,
        as a file piped in would be), and env added to its environment. What
        it prints is read as text, with Windows' line endings as \\n."""
        raw = subprocess.run(
            [sys.executable, "-m", "git_recommit", "-C", str(self.path), *args],
            capture_output=True,
            input=None if input is None else input.encode(),
            env={**os.environ, **(env or {})},
            timeout=60,
            check=False,
        )

        def text(output: bytes) -> str:
            return output.decode("utf-8", errors="replace").replace("\r\n", "\n")

        result = subprocess.CompletedProcess(raw.args, raw.returncode, text(raw.stdout), text(raw.stderr))
        if expect_success != (result.returncode == 0):
            pytest.fail(f"git recommit {' '.join(args)} exited {result.returncode}:\n{result.stdout}{result.stderr}")
        return result
