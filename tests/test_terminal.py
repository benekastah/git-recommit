"""git recommit run in a real (pseudo-)terminal, for what Textual's headless
test driver can't do, like suspending with ctrl+z."""

import os
import signal
import subprocess
import sys
import time

import pytest

from tests.helpers import Repo

if sys.platform == "win32":
    # Raised rather than pytest.skip(), so the type checker sees the rest as Unix only.
    raise pytest.skip.Exception("needs a Unix pseudo-terminal", allow_module_level=True)

import select

JOB_CONTROL = os.path.join(os.path.dirname(__file__), "job_control.py")

# Textual switches to the alternate screen when it starts or resumes, and back
# when it suspends or exits.
ENTER_SCREEN = b"\x1b[?1049h"
LEAVE_SCREEN = b"\x1b[?1049l"


class Terminal:
    """A command running as a job in a pseudo-terminal (see job_control.py)."""

    def __init__(self, *argv: str) -> None:
        self.fd, tty = os.openpty()
        environ = {**os.environ, "TERM": "xterm-256color"}
        self.shell = subprocess.Popen(
            [sys.executable, JOB_CONTROL, *argv], stdin=tty, stdout=tty, stderr=tty, env=environ, start_new_session=True
        )
        os.close(tty)
        self.output = b""
        self.expect(b"[pid ")
        self.expect(b"]")
        self.pid = int(self.consumed)

    def send(self, keys: str | bytes) -> None:
        os.write(self.fd, keys.encode() if isinstance(keys, str) else keys)

    def _read(self, deadline: float) -> bool:
        """Read what output there is by deadline; False once there's no more."""
        ready, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
        if ready:
            try:
                data = os.read(self.fd, 65536)
            except OSError:  # every process using the terminal has exited
                return False
            self.output += data
            return bool(data)
        return True

    def expect(self, pattern: bytes, timeout: float = 15) -> None:
        """Wait for pattern in the output since the last expect, and consume
        the output up to it, keeping what came before it in consumed."""
        deadline = time.monotonic() + timeout
        while (index := self.output.find(pattern)) < 0:
            if time.monotonic() > deadline or not self._read(deadline):
                pytest.fail(f"no {pattern!r} in the output, which ends {self.output[-2000:]!r}")
        self.consumed = self.output[:index]
        self.output = self.output[index + len(pattern) :]

    def state(self) -> str:
        """The job's state as ps shows it, e.g. T when it's stopped."""
        return subprocess.run(
            ["ps", "-o", "stat=", "-p", str(self.pid)], capture_output=True, text=True, check=False
        ).stdout

    def wait(self, timeout: float = 15) -> int:
        """Wait for the job to exit, and return its exit status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._read(min(deadline, time.monotonic() + 0.1))
            if (status := self.shell.poll()) is not None:
                return status
        pytest.fail(f"it didn't exit; the output ends {self.output[-2000:]!r}")

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.shell.kill()
        self.shell.wait()
        os.close(self.fd)


def test_starts_from_the_commits_being_rebuilt(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    repo.git("commit", "--quiet", "--amend", "--no-edit", "--author=Someone Else <else@example.com>")
    old_tree = repo.git("rev-parse", "HEAD^{tree}")
    term = Terminal(sys.executable, "-m", "git_recommit", "-C", str(repo.path), "HEAD~")
    try:
        term.expect(b"Rebuilding 1 commit after")
        term.send("w")
        term.expect(b"Replace the 1 commit")
        term.send("y")
        assert term.wait() == 0
    finally:
        term.close()
    assert repo.git("log", "--format=%s %an", "-1") == "wip Someone Else"
    assert repo.git("rev-parse", "HEAD^{tree}") == old_tree


def test_ctrl_z_suspends_and_resuming_reloads(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    term = Terminal(sys.executable, "-m", "git_recommit", "-C", str(repo.path), "HEAD~")
    try:
        term.expect(b"Rebuilding 1 commit after")
        term.send("C")
        term.expect(b"Clear")
        term.send("y")
        term.send("c")
        term.expect(b"Message for the new commit")
        term.send("Add a\r")
        term.expect(b"Created commit 1")

        term.send(b"\x1a")  # ctrl+z
        term.expect(LEAVE_SCREEN)
        term.expect(b"[stopped]")
        assert term.state().startswith("T")
        # While it's stopped, change a file.
        repo.write("a", "a\nmore\n")
        # Bring it back, as the shell's fg does.
        os.kill(term.pid, signal.SIGCONT)
        term.expect(ENTER_SCREEN)
        term.expect(b"Reloaded the changes")

        term.send("f")
        term.expect(b"Assigned to 1: 2 changes")
        term.send("w")
        term.expect(b"Replace the 1 commit")
        term.send("y")
        assert term.wait() == 0
    finally:
        term.close()

    assert repo.git("log", "--format=%s", "-1") == "Add a"
    assert repo.show("HEAD", "a") == "a\nmore\n"
