"""The plan, as `git recommit show` prints it: as text for people, and as
JSON for scripts and agents."""

from typing import Any

from git_recommit.board import line_text
from git_recommit.diff import numbered
from git_recommit.session import Session


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def _numbers(session: Session) -> dict[int, int]:
    """Each assigned change's commit's number."""
    numbers = {commit.key: n for n, commit in enumerate(session.plan.commits, start=1)}
    return {i: numbers[key] for i, key in session.plan.assignment.items()}


def summary(session: Session) -> list[str]:
    old = len(session.old_commits)
    lines = [f"Rebuilding {_plural(old, 'commit')} after {session.base[:7]}"]
    if session.uncommitted:
        lines[0] += ", and the uncommitted changes"
    lines += ["", "Commits:"]
    conflicts = {(c.commit.key, c.path) for c in session.conflicts()}
    for number, commit in enumerate(session.plan.commits, start=1):
        lines.append(f"  {number} {commit.subject}  ({_plural(session.plan.count(commit.key), 'change')})")
        lines.extend(f"      edits {path}" for path in commit.edits)
        lines.extend(f"      conflict in {path}" for key, path in sorted(conflicts) if key == commit.key)
    if not session.plan.commits:
        lines.append("  (none; add one with `git recommit commit add <message>`)")
    lines.append(f"Unassigned: {len(session.unassigned())}")
    return lines


def diff(session: Session) -> list[str]:
    """The changes, each marked with its commit's number (· for none), with
    the hunk and line numbers targets use."""
    numbers = _numbers(session)

    def marker(i: int | None) -> str:
        return "" if i is None else str(numbers.get(i, "·"))

    lines = []
    for file in session.files:
        lines.append(f"{file.status} {file.path}")
        if file.meta_id is not None:
            lines.append(f"{marker(file.meta_id):>3}  {file.path}:@0 {file.describe_meta()}")
        for h, hunk in enumerate(file.hunks, start=1):
            header = f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
            lines.append(f"     {header} {hunk.section}  ({file.path}:@{h})".rstrip())
            for line, old, new in numbered(hunk):
                lines.append(
                    f"{marker(line.id):>3} {old or '':>5} {new or '':>5} {line.kind}{line_text(line)}".rstrip()
                )
    return lines


def as_json(session: Session) -> dict[str, Any]:
    numbers = _numbers(session)
    conflicts = session.conflicts()
    files = []
    for file in session.files:
        changes: list[dict[str, Any]] = []
        if file.meta_id is not None:
            changes.append(
                {
                    "kind": "file",
                    "old": None,
                    "new": None,
                    "text": file.describe_meta(),
                    "hunk": 0,
                    "commit": numbers.get(file.meta_id),
                }
            )
        for h, hunk in enumerate(file.hunks, start=1):
            changes.extend(
                {
                    "kind": line.kind,
                    "old": old,
                    "new": new,
                    "text": line.text.decode(errors="replace"),
                    "hunk": h,
                    "commit": numbers.get(line.id),
                }
                for line, old, new in numbered(hunk)
                if line.id is not None
            )
        files.append({"path": file.path, "status": file.status, "binary": file.binary, "changes": changes})
    return {
        "base": session.base,
        "head": session.head,
        "uncommitted": session.uncommitted,
        "commits": [
            {
                "number": n,
                "message": commit.message,
                "author": None if commit.author is None else vars(commit.author),
                "changes": session.plan.count(commit.key),
                "edits": list(commit.edits),
                "conflicts": [c.path for c in conflicts if c.commit.key == commit.key],
            }
            for n, commit in enumerate(session.plan.commits, start=1)
        ],
        "files": files,
        "unassigned": len(session.unassigned()),
        "left_uncommitted": session.left_uncommitted(),
    }
