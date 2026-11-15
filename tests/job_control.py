"""Run a command in a terminal the way a shell runs a job, for tests.

Usage: python tests/job_control.py <command> [<arg>...]

Run with stdin, stdout and stderr on a pseudo-terminal, in a new session. It
makes the terminal its session's controlling terminal (size 120x40), then
runs the command in a process group of its own, in the foreground. It writes
"[pid <pid>]" when the command starts and "[stopped]" whenever it stops, and
exits with its exit status.

Running the command straight as the session's first process wouldn't do:
that process's group has no parent outside it in the session, which makes it
orphaned, and the kernel doesn't let an orphaned group's processes stop on
SIGTSTP, so ctrl+z couldn't stop it.
"""

import os
import signal
import struct
import sys

if sys.platform == "win32":
    raise SystemExit("needs a Unix terminal")

import fcntl
import termios


def main(argv: list[str]) -> int:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    # So it can give the job the terminal from outside the foreground.
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    job = os.fork()
    if job == 0:
        os.setpgid(0, 0)
        os.tcsetpgrp(0, os.getpid())
        signal.signal(signal.SIGTTOU, signal.SIG_DFL)
        os.execv(argv[0], argv)
    os.write(1, f"[pid {job}]\r\n".encode())
    while True:
        _, status = os.waitpid(job, os.WUNTRACED)
        if os.WIFSTOPPED(status):
            os.write(1, b"[stopped]\r\n")
        else:
            return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
