import io
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

import pytest
from git_client import CLOSE_FDS, Git
from git_client.testing import real_git

from git_recommit import cli
from git_recommit.git import GitClient
from git_recommit.session import Session

# The git the tests run, and git recommit too (conftest sets
# GIT_RECOMMIT_GIT): git itself, rather than whatever git is on PATH (e.g. a
# wrapper, like git-ai's, which would be slower).
GIT = real_git()


@dataclass
class Repo:
    path: pathlib.Path
    # What git recommit prints when it runs in the test's process (see
    # recommit), by file descriptor, so with what git's children print too.
    capture: pytest.CaptureFixture[str] | None = None

    @property
    def client(self) -> GitClient:
        return GitClient(self.path)

    def git(self, *args: str) -> str:
        return Git(self.path, program=GIT).run(*args)

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

    def stage_unlisted_name(self, name: bytes, content: bytes) -> None:
        """Stage a file with a name that may not be UTF-8, made with plumbing
        through stdin (a name can't be an argument on Windows, and some
        filesystems, e.g. macOS's, refuse such names); skip-worktree keeps git
        from seeing it as deleted from the working tree."""
        blob = self.client.hash_blob(content)
        self.client.run_bytes(
            "update-index", "--add", "--index-info", input=f"100644 {blob} 0\t".encode() + name + b"\n"
        )
        self.client.run_bytes("update-index", "--skip-worktree", "-z", "--stdin", input=name + b"\0")

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
        result = Git(self.path, program=GIT).process("show", f"{rev}:{name}", capture_output=True)
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
        """Run `git recommit <args>` (without a terminal), with input on its
        stdin (as UTF-8, with its line endings as they are, as a file piped in
        would be), and env added to its environment. What it prints is read
        as text, with Windows' line endings as \\n.

        It runs in the test's process, as starting Python is slow (on macOS
        and Windows most of all); but as a separate one with env (e.g. a
        locale, which Python only reads as it starts), or without capture."""
        argv = ["-C", str(self.path), *args]
        if env is None and self.capture is not None:
            result = self._recommit_here(argv, input, self.capture)
        else:
            result = self._recommit_apart(argv, input, env)
        if expect_success != (result.returncode == 0):
            pytest.fail(f"git recommit {' '.join(args)} exited {result.returncode}:\n{result.stdout}{result.stderr}")
        return result

    def _recommit_here(
        self, argv: list[str], input: str | None, capture: pytest.CaptureFixture[str]
    ) -> subprocess.CompletedProcess[str]:
        stdin = sys.stdin
        sys.stdin = io.TextIOWrapper(io.BytesIO(b"" if input is None else input.encode()))
        # What the test printed before, put back after (for its failure report).
        before = capture.readouterr()
        try:
            try:
                returncode = cli.main(argv)
            except SystemExit as e:
                # argparse's, for a bad command line, always with a number.
                if not isinstance(e.code, int):
                    raise
                returncode = e.code
            out, err = capture.readouterr()
        finally:
            sys.stdin = stdin
            sys.stdout.write(before.out)
            sys.stderr.write(before.err)
        return subprocess.CompletedProcess(argv, returncode, _text(out), _text(err))

    def _recommit_apart(
        self, argv: list[str], input: str | None, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        raw = subprocess.run(
            [sys.executable, "-m", "git_recommit", *argv],
            capture_output=True,
            input=None if input is None else input.encode(),
            env={**os.environ, **(env or {})},
            timeout=60,
            close_fds=CLOSE_FDS,
            check=False,
        )
        out, err = (output.decode("utf-8", errors="replace") for output in (raw.stdout, raw.stderr))
        return subprocess.CompletedProcess(raw.args, raw.returncode, _text(out), _text(err))


def _text(output: str) -> str:
    """What git recommit printed, with Windows' line endings as \\n."""
    return output.replace("\r\n", "\n")
