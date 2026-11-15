"""The plan of a recommit in progress, kept in a file between commands.

It's JSON in the git directory (`git rev-parse --git-path recommit/plan.json`),
so it's per worktree, and found without being named, like a rebase in
progress. Changes are stored by diff.change_keys, not ids, so a plan still
fits after the working tree (or HEAD) changes: loading it is like reloading.
"""

import base64
import json
import os
from typing import Any

from git_recommit.diff import change_keys
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
from git_recommit.plan import Author, Edit
from git_recommit.session import Session

VERSION = 1


def path(git: GitClient) -> str:
    return git.run("rev-parse", "--path-format=absolute", "--git-path", "recommit/plan.json")


def exists(git: GitClient) -> bool:
    return os.path.exists(path(git))


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _decode(text: str) -> bytes:
    return base64.b64decode(text)


def save(session: Session) -> None:
    plan = session.plan
    keys = change_keys(session.files)
    data = {
        "version": VERSION,
        "base": session.base,
        "head": session.head,
        "include_untracked": session.include_untracked,
        "root": session.root,
        "unchanged": sorted(session.unchanged),
        "commits": [
            {
                "message": commit.message,
                "author": None if commit.author is None else vars(commit.author),
                "old_message": commit.old_message,
                "edits": {
                    path: {
                        "basis": _encode(edit.basis),
                        "content": None if edit.content is None else _encode(edit.content),
                    }
                    for path, edit in commit.edits.items()
                },
            }
            for commit in plan.commits
        ],
        "origin": [
            {"change": [*keys[i][:2], _encode(keys[i][2]), keys[i][3]], "author": vars(author)}
            for i, author in session.origin.items()
        ],
        "assignment": [
            {"change": [*keys[i][:2], _encode(keys[i][2]), keys[i][3]], "commit": plan.index(key) + 1}
            for i, key in plan.assignment.items()
        ],
    }
    file = path(session.git)
    os.makedirs(os.path.dirname(file), exist_ok=True)
    # Written beside it, then moved into place, so it's never half written.
    with open(file + ".new", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(file + ".new", file)


def load(git: GitClient) -> Session:
    """The recommit in progress, with the diff as it is now."""
    if not exists(git):
        raise RecommitError("there's no recommit in progress; start one with `git recommit start <range>`")
    with open(path(git), encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    if data.get("version") != VERSION:
        raise RecommitError(f"{path(git)} is from another version of git recommit; abort it and start again")
    session = Session.load(git, None if data.get("root") else data["base"], data.get("include_untracked", False))
    for unchanged in data.get("unchanged", []):
        session.add_unchanged(unchanged)
    plan = session.plan
    for saved in data["commits"]:
        author = None if saved["author"] is None else Author(**saved["author"])
        commit = plan.add(saved["message"], author=author)
        commit.old_message = saved.get("old_message")
        for edited, edit in saved["edits"].items():
            content = None if edit["content"] is None else _decode(edit["content"])
            commit.edits[edited] = Edit(_decode(edit["basis"]), content)
    ids = {key: i for i, key in change_keys(session.files).items()}
    for made in data.get("origin", []):
        file, kind, text, n = made["change"]
        i = ids.get((file, kind, _decode(text), n))
        if i is not None:
            session.origin[i] = Author(**made["author"])
    for assigned in data["assignment"]:
        file, kind, text, n = assigned["change"]
        i = ids.get((file, kind, _decode(text), n))
        if i is not None:
            plan.assign([i], plan.commits[assigned["commit"] - 1].key)
    return session


def remove(git: GitClient) -> None:
    if exists(git):
        os.remove(path(git))
