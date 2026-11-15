import os
import shlex
import subprocess
import tempfile

from git_recommit.errors import RecommitError
from git_recommit.git import GitClient


def shell_quote(arg: str) -> str:
    # git runs editors with sh, even on Windows, where it brings its own.
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


def edit(git: GitClient, content: bytes, filename: str) -> bytes:
    """Open content in the user's editor (as git chooses it), in a file with
    filename's name so the editor knows what kind of file it is, and return what
    they saved."""
    editor = git.run("var", "GIT_EDITOR")
    with tempfile.TemporaryDirectory() as scratch:
        path = os.path.join(scratch, os.path.basename(filename) or "file")
        with open(path, "wb") as f:
            f.write(content)
        if subprocess.run(f"{editor} {shell_quote(path)}", shell=True, check=False).returncode != 0:
            raise RecommitError("the editor failed, so nothing was changed")
        with open(path, "rb") as f:
            return f.read()


def strip_comments(message: str) -> str:
    """A commit message as git would keep it: without # lines, trimmed."""
    return "\n".join(line for line in message.splitlines() if not line.startswith("#")).strip()
