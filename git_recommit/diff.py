"""The changes between two commits, split into the pieces a new commit can take.

Each added or removed line of a text file is a change of its own, with an id.
What can't be split into lines is a file's "meta" change, also with an id: the
whole of a binary file, symlink or submodule (or a change from one of those to
another kind), a file's mode, renaming it, and adding or deleting an empty
file.
"""

import re
from collections.abc import Callable, Container, Iterator
from dataclasses import dataclass, field
from itertools import count
from typing import Literal

from git_recommit.errors import RecommitError
from git_recommit.git import GitClient

LineKind = Literal["+", "-", " "]

HUNK_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
# Symlinks and submodules, which aren't split into lines.
SYMLINK_MODE, SUBMODULE_MODE = "120000", "160000"
SPECIAL_MODES = {SYMLINK_MODE, SUBMODULE_MODE}
NO_MODE = "000000"


@dataclass
class Line:
    kind: LineKind
    # The line as it is in the file, with its line ending (if it has one).
    text: bytes
    # None for a context line, which isn't a change.
    id: int | None = None


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    # What git shows after the @@ (e.g. the enclosing function).
    section: str
    lines: list[Line] = field(default_factory=list)

    @property
    def ids(self) -> list[int]:
        return [line.id for line in self.lines if line.id is not None]


@dataclass
class FileDiff:
    # Where it ends up: for a renamed file, its new path.
    path: str
    # A (added), D (deleted), M (modified), R (renamed) or T (changed kind,
    # e.g. a file to a symlink).
    status: str
    old_mode: str | None
    new_mode: str | None
    old_blob: str | None
    new_blob: str | None
    binary: bool
    hunks: list[Hunk] = field(default_factory=list)
    meta_id: int | None = None
    # The file's lines before the changes, for a text file.
    old_lines: list[bytes] = field(default_factory=list)
    # Where a renamed file was; it's there until a commit takes the rename.
    old_path: str | None = None

    @property
    def whole(self) -> bool:
        """Whether the file only changes as a whole, not line by line."""
        return self.binary or self.status == "T" or bool({self.old_mode, self.new_mode} & SPECIAL_MODES)

    @property
    def submodule(self) -> bool:
        """Whether it's a submodule (at either end), which can't be edited."""
        return SUBMODULE_MODE in (self.old_mode, self.new_mode)

    def describe_meta(self) -> str:
        """What the file's change to itself (its meta change) is."""
        renamed = [f"renamed from {self.old_path}"] if self.old_path is not None else []
        both = self.old_mode is not None and self.new_mode is not None
        mode = [f"mode {self.old_mode} -> {self.new_mode}"] if both and self.old_mode != self.new_mode else []
        if self.binary:
            return ", ".join(["binary", *renamed, *mode])
        if self.whole:
            modes = f"{self.old_mode or 'none'} -> {self.new_mode or 'none'}"
            return ", ".join([*renamed, modes] if not renamed or self.old_mode != self.new_mode else renamed)
        if self.status in ("A", "D"):
            return "added, empty" if self.status == "A" else "deleted, empty"
        return ", ".join([*renamed, *mode])

    def path_with(self, selected: Container[int]) -> str:
        """Where the file is with only the selected changes: for a renamed
        file, its old path unless the rename is selected."""
        return (
            self.old_path
            if self.old_path is not None and (self.meta_id is None or self.meta_id not in selected)
            else self.path
        )

    @property
    def ids(self) -> list[int]:
        ids = [i for hunk in self.hunks for i in hunk.ids] if not self.whole else []
        return ids if self.meta_id is None else [*ids, self.meta_id]


def numbered(hunk: Hunk) -> Iterator[tuple[Line, int | None, int | None]]:
    """Each line of the hunk, with its line numbers before and after the
    changes (None for a line that's only on one side)."""
    # A hunk that adds to an empty file starts at line 0.
    old, new = max(hunk.old_start, 1), max(hunk.new_start, 1)
    for line in hunk.lines:
        yield line, old if line.kind != "+" else None, new if line.kind != "-" else None
        old += line.kind != "+"
        new += line.kind != "-"


def split_lines(data: bytes) -> list[bytes]:
    """Split data into lines as git does, at each \\n, keeping it."""
    lines = data.split(b"\n")
    result = [line + b"\n" for line in lines[:-1]]
    if lines[-1]:
        result.append(lines[-1])
    return result


def join_lines(lines: list[bytes]) -> bytes:
    """Join lines, ending any but the last that lacks a line ending with one.

    A line lacks one when it was the last line of a file; taking some changes
    and not others can put another line after it.
    """
    return b"".join(
        line if line.endswith(b"\n") or i == len(lines) - 1 else line + b"\n" for i, line in enumerate(lines)
    )


def apply(diff: FileDiff, selected: Container[int]) -> bytes:
    """A text file's content with only the selected changes applied."""
    old, out, pos = diff.old_lines, [], 0
    for hunk in diff.hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        out.extend(old[pos:start])
        pos = start
        for line in hunk.lines:
            if line.kind == " ":
                out.append(old[pos])
                pos += 1
            elif line.kind == "-":
                if line.id is None or line.id not in selected:
                    out.append(old[pos])
                pos += 1
            elif line.id is not None and line.id in selected:
                out.append(line.text)
    out.extend(old[pos:])
    return join_lines(out)


def parse_hunks(patch_lines: list[bytes], next_id: Callable[[], int]) -> list[Hunk]:
    """The hunks in one file's part of a patch, giving each change an id."""
    hunks: list[Hunk] = []
    for raw in patch_lines:
        if match := HUNK_HEADER.match(raw):
            old_start, old_count, new_start, new_count, section = match.groups()
            hunks.append(
                Hunk(
                    int(old_start),
                    1 if old_count is None else int(old_count),
                    int(new_start),
                    1 if new_count is None else int(new_count),
                    section.decode(errors="replace").strip(),
                )
            )
        elif not hunks:
            continue  # the file's header
        elif raw.startswith(b"\\"):
            # "\ No newline at end of file", about the line before.
            if hunks[-1].lines:
                hunks[-1].lines[-1].text = hunks[-1].lines[-1].text.removesuffix(b"\n")
        elif raw[:1] in (b"+", b"-"):
            kind: LineKind = "+" if raw[:1] == b"+" else "-"
            hunks[-1].lines.append(Line(kind, raw[1:] + b"\n", next_id()))
        elif raw[:1] == b" ":
            hunks[-1].lines.append(Line(" ", raw[1:] + b"\n"))
    return hunks


def _patch_parts(patch: bytes) -> Iterator[list[bytes]]:
    """Split a patch into each file's part, as lines."""
    part: list[bytes] | None = None
    lines = patch.split(b"\n")
    if lines and not lines[-1]:
        lines.pop()
    for line in lines:
        if line.startswith(b"diff --git "):
            if part is not None:
                yield part
            part = []
        elif part is not None:
            part.append(line)
    if part is not None:
        yield part


RawEntry = tuple[str, str, str, str, str, str, str | None]


def _split_diff(out: bytes) -> tuple[list[RawEntry], set[str], bytes]:
    """`git diff-tree --raw --numstat -p -z` output, as: (old mode, new mode,
    old blob, new blob, status, path, old path) for each file, where the old
    path is only for a rename; the files git counts as binary (by their
    content, or their attributes), by new path; and the patch."""
    pos = 0

    def field() -> bytes:
        nonlocal pos
        end = out.index(b"\0", pos)
        value, pos = out[pos:end], end + 1
        return value

    entries: list[RawEntry] = []
    # ":<old mode> <new mode> <old blob> <new blob> <status>", then the path
    # (for a rename, the old path, then the new one).
    while out.startswith(b":", pos):
        old_mode, new_mode, old_blob, new_blob, status = field().decode().lstrip(":").split(" ")
        old_path = field().decode(errors="surrogateescape") if status[:1] == "R" else None
        path = field().decode(errors="surrogateescape")
        entries.append((old_mode, new_mode, old_blob, new_blob, status[:1], path, old_path))
    # A file's added and deleted lines (- and - if it's binary), and its path
    # (for a rename, empty, then its old path and its new one).
    binary: set[str] = set()
    for _ in entries:
        added, deleted, numstat_path = field().split(b"\t", 2)
        if not numstat_path:
            field()
            numstat_path = field()
        if (added, deleted) == (b"-", b"-"):
            binary.add(numstat_path.decode(errors="surrogateescape"))
    # Then an empty field, and the patch, which can have NULs in it (a file
    # with them that its attributes say is text).
    if entries and field():
        raise RecommitError("git diff-tree's output doesn't end its file list where expected")
    return entries, binary, out[pos:]


ChangeKey = tuple[str, str, bytes, int]


def change_keys(files: list[FileDiff]) -> dict[int, ChangeKey]:
    """A key for each change id that's the same for the same change in
    another diff of the same files (e.g. after the user changes one): its
    path, kind and text, and which of the file's changes with that kind and
    text it is."""
    keys: dict[int, ChangeKey] = {}
    for diff in files:
        seen: dict[tuple[str, bytes], int] = {}
        if diff.meta_id is not None:
            keys[diff.meta_id] = (diff.path, "meta", b"", 0)
        for hunk in diff.hunks if not diff.whole else []:
            for line in hunk.lines:
                if line.id is not None:
                    n = seen.get((line.kind, line.text), 0)
                    seen[line.kind, line.text] = n + 1
                    keys[line.id] = (diff.path, line.kind, line.text, n)
    return keys


def renames(git: GitClient) -> str:
    """How to have git diff-tree find renames as git diff does: unless
    diff.renames is off. (Copies, which diff.renames can ask for too, are
    left as added files, since what they copy stays where it is.)"""
    # As a bool, as true or false; or as it is (e.g. copies).
    return "--no-renames" if git.run("config", "--type=bool-or-str", "diff.renames", check=False) == "false" else "-M"


def load_diff(git: GitClient, base: str, tip: str) -> list[FileDiff]:
    """The changes from base to tip, file by file."""
    out = git.run_bytes(
        "diff-tree",
        # Even a submodule submodule.<name>.ignore leaves out, as even
        # plumbing does.
        "--ignore-submodules=none",
        "-r",
        renames(git),
        "--no-abbrev",
        "--raw",
        "--numstat",
        "-p",
        "-U3",
        "--full-index",
        "-z",
        base,
        tip,
    )
    entries, binary_paths, patch = _split_diff(out)
    # Both list the files in the same order, but the patch shows a change of
    # kind (status T, e.g. a file to a symlink) as two parts: a deletion, then
    # an addition. A file of either kind changes as a whole, so its parts
    # don't matter.
    parts = list(_patch_parts(patch))
    if len(parts) != len(entries) + sum(1 for entry in entries if entry[4] == "T"):
        raise RecommitError(f"git diff-tree listed {len(entries)} files, but its patch doesn't match")
    remaining = iter(parts)
    ids = count()
    files: list[FileDiff] = []
    for old_mode, new_mode, old_blob, new_blob, status, path, old_path in entries:
        part = next(remaining)
        if status == "T":
            part += next(remaining)
        diff = FileDiff(
            path=path,
            status=status,
            old_mode=None if old_mode == NO_MODE else old_mode,
            new_mode=None if new_mode == NO_MODE else new_mode,
            old_blob=None if status == "A" else old_blob,
            new_blob=None if status == "D" else new_blob,
            binary=path in binary_paths,
            old_path=old_path,
        )
        if diff.whole:
            diff.meta_id = next(ids)
        else:
            diff.hunks = parse_hunks(part, ids.__next__)
            if diff.old_blob is not None:
                diff.old_lines = split_lines(git.blob(diff.old_blob))
            if (
                (status in "AD" and not diff.hunks)
                or status == "R"
                or (status == "M" and diff.old_mode != diff.new_mode)
            ):
                diff.meta_id = next(ids)
        files.append(diff)
    return files
