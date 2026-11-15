import pathlib
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
        result = subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            pytest.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def write(self, name: str, content: str | bytes) -> None:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        path.write_bytes(content)

    def commit(self, message: str, **files: str | bytes | None) -> str:
        """Write (or with None, delete) the files, and commit everything."""
        for name, content in files.items():
            if content is None:
                (self.path / name).unlink()
            else:
                self.write(name, content)
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def show(self, rev: str, name: str) -> str | None:
        """The file in rev, or None if it isn't there."""
        result = subprocess.run(["git", "show", f"{rev}:{name}"], cwd=self.path, capture_output=True, check=False)
        return result.stdout.decode() if result.returncode == 0 else None

    def session(self, spec: str) -> Session:
        return Session.load(self.client, spec)

    def recommit(self, *args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
        """Run `git recommit <args>` as a separate process (without a terminal)."""
        result = subprocess.run(
            [sys.executable, "-m", "git_recommit", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if expect_success != (result.returncode == 0):
            pytest.fail(f"git recommit {' '.join(args)} exited {result.returncode}:\n{result.stdout}{result.stderr}")
        return result
