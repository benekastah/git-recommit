import os
import shlex
import subprocess
import sys
import tempfile

from git_client import encode

from git_recommit.errors import RecommitError
from git_recommit.git import GitClient


def shell_quote(arg: str) -> str:
    """arg, quoted for the shell subprocess runs a command with: cmd.exe on
    Windows, sh elsewhere."""
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


def edit(git: GitClient, content: bytes, filename: str) -> bytes:
    """Open content in the user's editor (as git chooses it), in a file with
    filename's name so the editor knows what kind of file it is, and return what
    they saved."""
    editor = git.run("var", "GIT_EDITOR")
    with tempfile.TemporaryDirectory() as scratch:
        path = os.path.join(scratch, _local_name(filename))
        with open(path, "wb") as f:
            f.write(content)
        if subprocess.run(f"{editor} {shell_quote(path)}", shell=True, check=False).returncode != 0:
            raise RecommitError("the editor failed, so nothing was changed")
        with open(path, "rb") as f:
            return f.read()


def _local_name(path: str) -> str:
    """The name of a path git gave (as text), as a name in the filesystem:
    with the same bytes, whatever the locale's encoding; or on Windows, if it
    isn't UTF-8 (which a name there must be), just "file"."""
    name = os.path.basename(path) or "file"
    if sys.platform != "win32":
        return os.fsdecode(encode(name))
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return "file"
    return name


# git's scissors line (for commit.cleanup=scissors), after the comment character.
SCISSORS = "------------------------ >8 ------------------------"
# What core.commentChar=auto chooses from, in order, as git does.
AUTO_COMMENT_CHARS = "#;@!$%^&|:"


class MessageCleanup:
    """A commit message as it opens in the user's editor, and as it's kept
    after, as `git commit` does it: by commit.cleanup, with core.commentChar
    (or the first character no line of the message starts with, for auto)."""

    def __init__(self, git: GitClient, message: str) -> None:
        self.git = git
        self.message = message
        self.mode = git.run("config", "commit.cleanup", check=False) or "default"
        comment = git.run("config", "core.commentChar", check=False) or "#"
        if comment == "auto":
            starts = {line[:1] for line in message.splitlines()}
            comment = next((c for c in AUTO_COMMENT_CHARS if c not in starts), "#")
        self.comment = comment

    def template(self) -> str:
        """The message, and help for the user, as comments that are left out."""
        c = self.comment
        message = self.message if self.message.endswith("\n") else self.message + "\n"
        if self.mode in ("whitespace", "verbatim"):
            # Comments are kept, so there's no help to show.
            return message
        if self.mode == "scissors":
            return f"{message}\n{c} {SCISSORS}\n{c} Everything below the line above is left out.\n"
        return f"{message}\n{c} Write the commit's message. Lines starting with {c} are left out.\n"

    def clean(self, text: str) -> str:
        """The message kept from what the user saved."""
        if self.mode == "verbatim":
            return text
        if self.mode == "scissors":
            lines = text.splitlines(keepends=True)
            cut = next((i for i, line in enumerate(lines) if line.rstrip("\n") == f"{self.comment} {SCISSORS}"), None)
            text = "".join(lines[:cut])
        strip = ["--strip-comments"] if self.mode not in ("whitespace", "scissors") else []
        return self.git.run("-c", f"core.commentChar={self.comment}", "stripspace", *strip, input=text)
