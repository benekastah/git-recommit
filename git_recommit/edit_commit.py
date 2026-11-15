"""`git recommit --edit <commit>`: stop at a commit to change it, then replay
the later ones on top, as `git rebase -i` with that commit marked "edit" does."""

import sys

from git_recommit.editor import shell_quote
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient


def edit_commit(git: GitClient, rev: str) -> bool:
    sha = git.commit_sha(rev)
    if sha is None:
        raise RecommitError(f"{rev} isn't a commit")
    if not git.is_ancestor(sha, "HEAD"):
        raise RecommitError(f"{rev} isn't on the current branch")
    if git.run("rev-list", "--merges", f"{sha}..HEAD"):
        raise RecommitError(f"there are merge commits after {rev}, which can't be replayed")
    # git runs the sequence editor with the todo list's path after these arguments.
    editor = " ".join(shell_quote(arg) for arg in (sys.executable, "-m", "git_recommit.sequence_editor", sha))
    onto = [f"{sha}^"] if git.commit_sha(f"{sha}^") else ["--root"]
    print(
        "When git stops at the commit, change it and `git commit --amend`, then run `git rebase --continue` "
        "to replay the later commits (resolving any conflicts as it asks)."
    )
    return git.passthrough("rebase", "--interactive", *onto, env={"GIT_SEQUENCE_EDITOR": editor})
