"""A sequence editor for `git rebase -i` that marks one commit "edit".

Usage: python -m git_recommit.sequence_editor <commit sha> <todo file>
"""

import sys


def mark_edit(todo: str, sha: str) -> str | None:
    """todo with sha's pick line changed to edit, or None if it has none."""
    lines = todo.splitlines(keepends=True)
    for index, line in enumerate(lines):
        words = line.split(maxsplit=2)
        if len(words) >= 2 and words[0] in ("pick", "p") and sha.startswith(words[1]):
            lines[index] = "edit " + line.split(maxsplit=1)[1]
            return "".join(lines)
    return None


def main(argv: list[str]) -> int:
    sha, path = argv
    # As it is (newline=""): with no \r\n for each \n on Windows, and the
    # bytes of commit subjects that aren't UTF-8 kept.
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        todo = f.read()
    marked = mark_edit(todo, sha)
    if marked is None:
        print(f"git-recommit: {sha} isn't in the rebase's todo list", file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(marked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
