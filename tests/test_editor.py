import os
import subprocess
import sys

import pytest

from git_recommit.editor import MessageCleanup
from tests.helpers import Repo

MESSAGE = "Fix the parser\n\n#123 was the bug.\n"


def edited(repo: Repo, message: str = MESSAGE, change: str = "") -> tuple[str, str]:
    """The message as it opens in the editor, and as it's kept after the
    editor saves it (with change added)."""
    cleanup = MessageCleanup(repo.client, message)
    opened = cleanup.template()
    return opened, cleanup.clean(opened + change)


def test_by_default_lines_starting_with_hash_are_comments(repo: Repo) -> None:
    opened, kept = edited(repo, change="# a note to self\n")
    assert "\n# " in opened
    # As git does when it opens an editor: even #123 is a comment.
    assert kept == "Fix the parser"


def test_core_comment_char(repo: Repo) -> None:
    repo.git("config", "core.commentChar", ";")
    opened, kept = edited(repo, change="; a note to self\n")
    assert "\n; " in opened
    assert kept == "Fix the parser\n\n#123 was the bug."


def test_core_comment_char_auto_picks_one_the_message_doesnt_use(repo: Repo) -> None:
    repo.git("config", "core.commentChar", "auto")
    opened, kept = edited(repo)
    assert kept == "Fix the parser\n\n#123 was the bug."
    assert "\n# " not in opened


@pytest.mark.parametrize(
    ("mode", "kept"),
    [
        ("whitespace", "Fix the parser\n\n#123 was the bug.\n\n# not a comment"),
        ("verbatim", "Fix the parser\n\n#123 was the bug.\n\n# not a comment  \n"),
    ],
)
def test_commit_cleanup_that_keeps_comments(repo: Repo, mode: str, kept: str) -> None:
    repo.git("config", "commit.cleanup", mode)
    opened, cleaned = edited(repo, change="\n# not a comment  \n")
    # No help text to keep, then.
    assert opened == MESSAGE
    assert cleaned == kept


def test_commit_cleanup_scissors(repo: Repo) -> None:
    repo.git("config", "commit.cleanup", "scissors")
    opened, kept = edited(repo)
    assert ">8" in opened
    assert kept == "Fix the parser\n\n#123 was the bug."


def test_editing_a_file_whose_name_isnt_ascii_whatever_the_locale(repo: Repo) -> None:
    # In a process of its own, since the locale's encoding is set as Python starts.
    script = "from git_recommit.editor import edit\nfrom git_recommit.git import GitClient\n"
    script += "import sys\nsys.stdout.buffer.write(edit(GitClient(sys.argv[1]), b'kept', 'dir/caf\\u00e9.txt'))\n"
    env = {**os.environ, "PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C", "GIT_EDITOR": "true"}
    result = subprocess.run([sys.executable, "-c", script, str(repo.path)], capture_output=True, env=env, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b"kept"
