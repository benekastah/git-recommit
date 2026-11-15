"""The state of the TUI, apart from how it's drawn: the rows of the diff, the
cursor and selection, and the commit being worked on."""

from dataclasses import dataclass
from typing import Literal

from git_recommit.diff import FileDiff, Line
from git_recommit.errors import RecommitError
from git_recommit.plan import NewCommit
from git_recommit.session import Session

RowKind = Literal["file", "hunk", "line"]
# Control characters (but tab) shown as "?", so they don't upset the terminal.
CONTROL = {**{i: "?" for i in range(32)}, ord("\t"): "\t", 127: "?"}


@dataclass(frozen=True)
class Row:
    kind: RowKind
    file: int
    hunk: int | None = None
    line: Line | None = None
    # A line's numbers in the file before and after the changes (None for a
    # line that's only on one side).
    old_number: int | None = None
    new_number: int | None = None


def build_rows(files: list[FileDiff]) -> list[Row]:
    rows = []
    for f, diff in enumerate(files):
        rows.append(Row("file", f))
        for h, hunk in enumerate(diff.hunks):
            rows.append(Row("hunk", f, h))
            # A hunk that adds to an empty file starts at line 0.
            old, new = max(hunk.old_start, 1), max(hunk.new_start, 1)
            for line in hunk.lines:
                rows.append(
                    Row("line", f, h, line, old if line.kind != "+" else None, new if line.kind != "-" else None)
                )
                old += line.kind != "+"
                new += line.kind != "-"
    return rows


def line_text(line: Line) -> str:
    return line.text.decode(errors="replace").rstrip("\r\n").translate(CONTROL).expandtabs(4)


class Board:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.plan = session.plan
        self.rows = build_rows(session.files)
        self.cursor = 0
        # Where a range selection (from `v`) started, if one is going.
        self.anchor: int | None = None
        # The key of the commit that space assigns to, edits apply to, and so on.
        self.active: int | None = None
        self.message = "Press c to create a commit, then assign changes to it. ? shows every key."
        # A row to scroll to the top of the screen, once; the view clears it.
        self.scroll_top: int | None = None

    # Where things are.

    @property
    def selection(self) -> tuple[int, int]:
        """The first and last rows selected: the range, or the cursor's row
        and, on a file's or hunk's header, the rest of the file or hunk."""
        if self.anchor is None:
            return self.cursor, self.extent(self.cursor) if self.rows else self.cursor
        return min(self.anchor, self.cursor), max(self.anchor, self.cursor)

    def extent(self, index: int) -> int:
        """The last row that row index covers: a file's or hunk's last line."""
        row = self.rows[index]
        if row.kind == "line":
            return index
        end = index + 1
        while end < len(self.rows) and not self._ends(row, self.rows[end]):
            end += 1
        return end - 1

    def _ends(self, start: Row, row: Row) -> bool:
        return row.kind == "file" or (start.kind == "hunk" and row.kind == "hunk")

    def row_ids(self, index: int) -> list[int]:
        """The changes a row stands for: a file's, a hunk's or a line's."""
        row = self.rows[index]
        diff = self.session.files[row.file]
        if row.kind == "file":
            return diff.ids
        if row.kind == "hunk":
            assert row.hunk is not None
            return diff.hunks[row.hunk].ids
        assert row.line is not None
        return [] if row.line.id is None else [row.line.id]

    def selected_ids(self) -> list[int]:
        first, last = self.selection
        seen: dict[int, None] = {}
        for index in range(first, last + 1):
            seen.update(dict.fromkeys(self.row_ids(index)))
        return list(seen)

    def current_file(self) -> FileDiff:
        return self.session.files[self.rows[self.cursor].file]

    def number(self, key: int) -> int:
        """The commit's place in the list, from 1."""
        return self.plan.index(key) + 1

    def active_commit(self) -> NewCommit:
        if self.active is None:
            raise RecommitError("there's no commit yet; press c to create one")
        return self.plan.get(self.active)

    # Moving.

    def goto(self, index: int) -> None:
        if self.rows:
            self.cursor = max(0, min(len(self.rows) - 1, index))

    def move(self, delta: int) -> None:
        self.goto(self.cursor + delta)

    def jump(self, kind: RowKind, direction: int) -> None:
        """Move to the next (or with -1, previous) file or hunk. Moving by hunk
        stops at a file only if it has no hunks (e.g. a binary file)."""
        index = self.cursor + direction
        while 0 <= index < len(self.rows):
            row = self.rows[index]
            if row.kind == kind or (kind == "hunk" and row.kind == "file" and not self.session.files[row.file].hunks):
                self.cursor = index
                # Show what was jumped to below it, and for a file's first
                # hunk, the file's name above.
                above = self.rows[index - 1] if index else None
                self.scroll_top = index - 1 if row.kind == "hunk" and above and above.kind == "file" else index
                return
            index += direction

    def goto_file(self, index: int) -> None:
        """Move to the header of the file at index in the session's files,
        and show it at the top of the screen."""
        for row_index, row in enumerate(self.rows):
            if row.kind == "file" and row.file == index:
                self.cursor = self.scroll_top = row_index
                self.anchor = None
                return

    def next_unassigned(self) -> None:
        for offset in range(1, len(self.rows) + 1):
            index = (self.cursor + offset) % len(self.rows)
            row = self.rows[index]
            if row.line and row.line.id is not None and row.line.id not in self.plan.assignment:
                self.cursor = index
                return
        self.message = "Every change is assigned."

    def toggle_range(self) -> None:
        self.anchor = self.cursor if self.anchor is None else None

    def reload(self, quiet: bool = False) -> None:
        """Reload the session's diff (see Session.reload), keeping the cursor
        in the same file, and as near the same place in it as there is. If
        quiet, say nothing when nothing has changed."""
        if not self.rows:
            return
        path = self.current_file().path
        start = next(i for i in range(self.cursor, -1, -1) if self.rows[i].kind == "file")
        offset = self.cursor - start
        try:
            changed = self.session.reload()
        except RecommitError as e:
            self.message = str(e)
            return
        if not changed:
            if not quiet:
                self.message = "Nothing has changed."
            return
        self.rows = build_rows(self.session.files)
        self.anchor = None
        files = [i for i, row in enumerate(self.rows) if row.kind == "file"]
        same = next((i for i in files if self.session.files[self.rows[i].file].path == path), None)
        self.cursor = 0 if same is None else min(same + offset, self.extent(same))
        self.message = "Reloaded the changes; assignments of changes that are still there are kept."

    # Changing the plan.

    def assign(self, key: int | None) -> None:
        """Assign the selected changes to the commit with key (None: unassign),
        then move past them."""
        self._assign(self.selected_ids(), key, self.extent(self.selection[1]))

    def assign_enclosing(self, kind: RowKind, key: int | None) -> None:
        """Assign the whole file (or hunk) the cursor is in, then move past it."""
        index = self.cursor
        while index > 0 and self.rows[index].kind not in (kind, "file"):
            index -= 1
        if self.rows and self.rows[index].kind != kind:
            # A file's header row, when assigning a hunk: there's no hunk to take.
            self.message = "Move to a hunk to assign it."
            return
        self._assign(self.row_ids(index) if self.rows else [], key, self.extent(index))

    def _assign(self, ids: list[int], key: int | None, last: int) -> None:
        if not ids:
            self.message = "There's no change here to assign."
            return
        self.plan.assign(ids, key)
        self.anchor = None
        self.goto(last + 1)
        what = "Unassigned" if key is None else f"Assigned to {self.number(key)}:"
        self.message = f"{what} {len(ids)} change{'s' if len(ids) != 1 else ''}."

    def assign_number(self, number: int) -> None:
        if not 1 <= number <= len(self.plan.commits):
            self.message = f"There's no commit {number}."
            return
        self.active = self.plan.commits[number - 1].key
        self.assign(self.active)

    def add_commit(self, message: str) -> None:
        """Add a commit after the active one, and make it active."""
        self.active = self.plan.add(message, after=self.active).key
        self.message = f"Created commit {self.number(self.active)}."

    def remove_active(self) -> None:
        commit = self.active_commit()
        index = self.plan.index(commit.key)
        self.plan.remove(commit.key)
        commits = self.plan.commits
        self.active = commits[min(index, len(commits) - 1)].key if commits else None
        self.message = f"Deleted {commit.subject!r}; its changes are unassigned."

    def move_active(self, delta: int) -> None:
        self.plan.move(self.active_commit().key, delta)

    def cycle_active(self, delta: int) -> None:
        commits = self.plan.commits
        if commits:
            index = 0 if self.active is None else (self.plan.index(self.active) + delta) % len(commits)
            self.active = commits[index].key
