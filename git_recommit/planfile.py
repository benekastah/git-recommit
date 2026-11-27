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
from git_recommit.revisions import GIT_PATHS
from git_recommit.session import Session

VERSION = 1


def path(git: GitClient) -> str:
    # With the others, as each subcommand asks for this first.
    return git.git_paths("recommit/plan.json", *GIT_PATHS)[0]


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
        # The range again, ending where it did (which may be below HEAD).
        "range": f"{session.base}..{session.end}",
        "unchanged": sorted(session.unchanged),
        "commits": [
            {
                "message": commit.message,
                "author": None if commit.author is None else vars(commit.author),
                "old_message": commit.old_message,
                "edits": {path: _save_edit(session.git, edit) for path, edit in commit.edits.items()},
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
    spec = None if data.get("root") else data.get("range", data["base"])
    session = Session.load(git, spec, data.get("include_untracked", False))
    for unchanged in data.get("unchanged", []):
        session.add_unchanged(unchanged)
    plan = session.plan
    for saved in data["commits"]:
        author = None if saved["author"] is None else Author(**saved["author"])
        commit = plan.add(saved["message"], author=author)
        commit.old_message = saved.get("old_message")
        for edited, edit in saved["edits"].items():
            commit.edits[edited] = _load_edit(git, edit)
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


def _save_edit(git: GitClient, edit: Edit) -> dict[str, Any]:
    """An edit as JSON. One of a file that changes as a whole keeps blob ids
    (see plan.Edit), of blobs nothing refers to, which git gc can drop; so
    it's saved with their contents, as a text file's edit is (null where it
    keeps no blob, for no file)."""
    saved: dict[str, Any]
    if not edit.blob:
        saved = {"basis": _encode(edit.basis), "content": None if edit.content is None else _encode(edit.content)}
    else:

        def content(sha: bytes | None) -> str | None:
            return _encode(git.blob(sha.decode())) if sha else None

        saved = {"basis": content(edit.basis), "content": content(edit.content), "blob": True}
    if edit.mode is not None:
        saved["mode"] = edit.mode
    return saved


def _load_edit(git: GitClient, saved: dict[str, Any]) -> Edit:
    mode = saved.get("mode")
    if not saved.get("blob"):
        content = None if saved["content"] is None else _decode(saved["content"])
        return Edit(_decode(saved["basis"]), content, mode=mode)

    def sha(encoded: str | None) -> bytes:
        return b"" if encoded is None else git.hash_blob(_decode(encoded)).encode()

    # No content deletes the file; no basis is the file not being there.
    return Edit(sha(saved["basis"]), None if saved["content"] is None else sha(saved["content"]), blob=True, mode=mode)


def remove(git: GitClient) -> None:
    if exists(git):
        os.remove(path(git))
