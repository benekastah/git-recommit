"""The TUI, drawn with Textual. What it shows and does to the plan is in
board.py; this is how it's drawn, and the keys."""

from collections.abc import Callable
from typing import ClassVar, Literal

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Region, Size
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Input, Static

from git_recommit import commands, editor, show, validate, writing
from git_recommit.board import Board, line_text
from git_recommit.diff import FileDiff
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
from git_recommit.session import Session

# Commit n's color is COLORS[(n - 1) % len(COLORS)].
COLORS = ["green", "yellow", "blue", "magenta", "cyan", "red", "bright_green", "bright_yellow", "bright_blue"]
CURSOR = Style(bgcolor="grey30")
SELECTED = Style(bgcolor="grey19")
LINE_STYLES = {"+": Style(color="green"), "-": Style(color="red"), " ": Style(dim=True)}
FILE_STYLE = Style(bold=True)
FILE_BACKGROUND = Style(bgcolor="grey15")
HUNK_STYLE = Style(color="cyan")
META_STYLE = Style(color="yellow", italic=True)
NUMBER_STYLE = Style(dim=True)


class Terminal:
    """What the app does in the terminal itself, with the TUI suspended: the
    user's editor and the --validate command. Tests replace it."""

    def __init__(self, git: GitClient, validate_command: str | None) -> None:
        self.git = git
        self.validate_command = validate_command

    def edit(self, app: App[object], content: bytes, filename: str) -> bytes:
        with app.suspend():
            return editor.edit(self.git, content, filename)

    def validate(self, app: App[object], commits: list[tuple[str, str]]) -> validate.Outcome:
        assert self.validate_command is not None
        with app.suspend():
            return validate.run(self.git, self.validate_command, commits)


def commit_style(number: int) -> Style:
    return Style(color=COLORS[(number - 1) % len(COLORS)], bold=True)


class DiffView(ScrollView, can_focus=True):
    """The changes, a row per file, hunk and line, each marked with the commit
    that takes it."""

    def __init__(self, board: Board) -> None:
        super().__init__()
        self.board = board
        self.rebuild()

    def rebuild(self) -> None:
        """Work out the rows' text and sizes again, for new rows."""
        board = self.board
        self._texts = [self._text(i) for i in range(len(board.rows))]
        numbers = [n for row in board.rows for n in (row.old_number, row.new_number) if n is not None]
        self._digits = len(str(max(numbers, default=0)))
        # The marker, both line numbers and the bar after them.
        self._gutter = 3 + 2 * (self._digits + 1) + 2
        self._width = self._gutter + max((cell_len(text) for text in self._texts), default=0) + 4

    def _text(self, index: int) -> str:
        row = self.board.rows[index]
        diff = self.board.session.files[row.file]
        if row.kind == "file":
            note = " (binary)" if diff.binary else " (the range doesn't change it)" if diff.status == "=" else ""
            path = f"{diff.old_path} -> {diff.path}" if diff.old_path is not None else diff.path
            return f"{diff.status} {path}{note}"
        if row.kind == "meta":
            return f"  {diff.describe_meta()}"
        if row.kind == "hunk":
            assert row.hunk is not None
            hunk = diff.hunks[row.hunk]
            return f" @@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@ {hunk.section} "
        assert row.line is not None
        return row.line.kind + line_text(row.line)

    class Clicked(Message):
        """The user clicked the row at index, with shift held or not."""

        def __init__(self, index: int, shift: bool) -> None:
            super().__init__()
            self.index = index
            self.shift = shift

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is not None and 0 <= offset.y + self.scroll_offset.y < len(self.board.rows):
            self.post_message(self.Clicked(offset.y + self.scroll_offset.y, event.shift))

    def update_rows(self) -> None:
        self.virtual_size = Size(self._width, len(self.board.rows))
        if self.board.scroll_top is not None:
            self.scroll_to(y=self.board.scroll_top, animate=False, immediate=True)
            self.board.scroll_top = None
        else:
            self.scroll_to_region(Region(0, self.board.cursor, 1, 1), animate=False, x_axis=False, immediate=True)
        self.refresh()

    def _marker(self, index: int) -> tuple[str, Style]:
        """Which commit takes the row's changes: its number, · for none, or
        * for more than one."""
        ids = self.board.row_ids(index)
        if not ids:
            return "   ", Style()
        keys = {self.board.plan.assignment.get(i) for i in ids}
        if keys == {None}:
            return " · ", Style(dim=True)
        if len(keys) > 1:
            return " * " if None not in keys else " ~ ", Style(bold=True)
        (key,) = keys
        assert key is not None
        number = self.board.number(key)
        return f"{number:>2} ", commit_style(number)

    def _numbers(self, index: int) -> Segment:
        """A line's numbers before and after, then a bar."""
        row = self.board.rows[index]
        if row.kind != "line":
            return Segment(" " * (self._gutter - 3))
        old, new = ("" if n is None else str(n) for n in (row.old_number, row.new_number))
        return Segment(f"{old:>{self._digits}} {new:>{self._digits}} │ ", NUMBER_STYLE)

    def _extra(self, index: int) -> list[Segment]:
        """After a file's name, whether the active commit edits it."""
        row = self.board.rows[index]
        if row.kind != "file" or self.board.active is None:
            return []
        path = self.board.session.files[row.file].path
        if path in self.board.plan.get(self.board.active).edits:
            return [Segment("  (edited in this commit)", Style(color="yellow", italic=True))]
        return []

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        index = scroll_y + y
        width = self.size.width
        if index >= len(self.board.rows):
            return Strip.blank(width)
        row = self.board.rows[index]
        first, last = self.board.selection
        background = CURSOR if index == self.board.cursor else SELECTED if first <= index <= last else Style()
        if row.kind == "file":
            style = FILE_STYLE
        elif row.kind == "meta":
            style = META_STYLE
        elif row.kind == "hunk":
            style = HUNK_STYLE
        else:
            assert row.line is not None
            style = LINE_STYLES[row.line.kind]
        marker, marker_style = self._marker(index)
        if row.kind == "hunk":
            # A rule across the panel, so it's clear where each hunk starts.
            rule = "─" * (self._gutter - 3)
            text = Segment(rule + self._texts[index] + "─" * (self._width + width), style)
            segments = [Segment(marker, marker_style), text]
        else:
            segments = [Segment(marker, marker_style), self._numbers(index), Segment(self._texts[index], style)]
            segments.extend(self._extra(index))
        strip = Strip(segments).crop(scroll_x, scroll_x + width).extend_cell_length(width)
        if row.kind == "file" and background == Style():
            background = FILE_BACKGROUND
        return strip.apply_style(background)


# What a line of the sidebar is about: a commit (by key) or a file (by index).
Target = tuple[Literal["commit", "file"], int] | None


class SidebarText:
    """The sidebar's text, built a section at a time, and what each of its
    lines is about. Lines don't wrap (see the CSS), so each is one line on
    screen, as targets says."""

    def __init__(self) -> None:
        self.text = Text()
        self.targets: list[Target] = []

    def add(self, text: str, style: Style | str = "", target: Target = None) -> None:
        self.text.append(text, style)
        self.targets.extend([target] * text.count("\n"))

    def heading(self, title: str, style: Style | str = "bold underline") -> None:
        """Start a section, after a blank line unless it's the first."""
        if self.targets:
            self.add("\n")
        self.add(f"{title}\n", style)


class Sidebar(Static):
    """The new commits, the old ones, and the files. Clicking a commit makes
    it the active one; clicking a file goes to it."""

    class Chosen(Message):
        def __init__(self, target: Target) -> None:
            super().__init__()
            self.target = target

    def __init__(self, id: str) -> None:
        super().__init__(id=id)
        # What each line is about.
        self.targets: list[Target] = []

    def show(self, sidebar: SidebarText) -> None:
        # Without the last line's line ending, which would show as a blank line.
        self.update(sidebar.text[:-1] if sidebar.text.plain.endswith("\n") else sidebar.text)
        self.targets = sidebar.targets

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is not None and 0 <= offset.y < len(self.targets) and self.targets[offset.y] is not None:
            self.post_message(self.Chosen(self.targets[offset.y]))


class InputScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.prompt)
            yield Input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.text + "\n\n[b]y[/b]es / [b]n[/b]o")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class ReloadScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter,escape", "close", "Continue")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static(self.text, id="reload-message", markup=False)
            yield Static("[b]Enter[/b] to continue.")

    def action_close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(HELP)

    def action_close(self) -> None:
        self.dismiss(None)


# The key bar along the top.
KEYS = (
    "[b]space[/b] assign line/range  [b]a[/b] hunk  [b]f[/b] file  [b]1-9[/b]/[b]#[/b] to commit n  [b]x[/b] unassign  "
    "[b]V[/b] select lines  [b]}/{[/b] next/previous hunk  [b].[/b] next unassigned\n"
    "[b]c[/b] new commit  [b]tab/shift+tab[/b] next/previous commit  [b]J/K[/b] move commit  [b]r[/b] reword  [b]D[/b] delete  "
    "[b]e[/b] edit file  [b]C[/b] clear  [b]O[/b] original  [b]w[/b] write  [b]q[/b] quit  [b]?[/b] help"
)

HELP = """\
[b]Moving[/b]
  j/k, ↓/↑      next/previous row         g/G      first/last row
  ]/[           next/previous file        }/{      next/previous hunk
  PgDn/PgUp     a page down/up            .        next unassigned change
  h/l, ←/→      scroll left/right
  V (or v)      start or end selecting lines, as in vim (Esc to cancel)

[b]Assigning[/b] to the active commit (the one marked ▶ in the sidebar)
  space/enter   the row (a line, or on a header, its hunk or file), or the selected range
  a             the whole hunk the cursor is in
  f             the whole file the cursor is in
  1-9           the row or range, to that commit (and make it active)
  #             the row or range, to the commit whose number you type
  x/0           unassign the row or range

[b]Commits[/b] (the active one is marked ▶)
  c             create a commit after the active one
  C             clear every commit, to start from scratch
  O             go back to the commits being rebuilt (as they were at the start)
  tab/S-tab     make the next/previous commit active
  J/K           move the active commit later/earlier
  r             reword the active commit's message in your editor
  A             set the active commit's author (Name <email>; empty for you)
  D             delete the active commit; its changes become unassigned
  e             edit this file as it is in the active commit, in your editor
                (the edit is carried into later commits)
  E             drop the active commit's edit of this file
  +             edit a file the range doesn't change (or that isn't there), as e

  R             reload the changes, after changing files (ctrl+z does too, on resuming)
  ctrl+z        suspend git recommit, to change files; `fg` brings it back
  w             rebuild the commits (and run --validate on each)
  q             quit without changing anything
"""


class BoardScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j,down", "move(1)", "Down", show=False),
        Binding("k,up", "move(-1)", "Up", show=False),
        Binding("pagedown,ctrl+d", "page(1)", "Page down", show=False),
        Binding("pageup,ctrl+u", "page(-1)", "Page up", show=False),
        Binding("g,home", "top", "Top", show=False),
        Binding("G,end", "bottom", "Bottom", show=False),
        Binding("right_square_bracket", "jump('file', 1)", "Next file", show=False),
        Binding("left_square_bracket", "jump('file', -1)", "Previous file", show=False),
        Binding("right_curly_bracket", "jump('hunk', 1)", "Next hunk", show=False),
        Binding("left_curly_bracket", "jump('hunk', -1)", "Previous hunk", show=False),
        Binding("full_stop", "next_unassigned", "Next unassigned", show=False),
        Binding("space,enter", "assign", "Assign line/range"),
        Binding("a", "assign_enclosing('hunk')", "Assign hunk"),
        Binding("f", "assign_enclosing('file')", "Assign file"),
        Binding("V,v", "range", "Select lines"),
        Binding("h,left", "scroll_sideways(-1)", "Left", show=False),
        Binding("l,right", "scroll_sideways(1)", "Right", show=False),
        Binding("escape", "clear", "Clear selection", show=False),
        *(Binding(str(n), f"assign_number({n})", f"Assign to {n}", show=False) for n in range(1, 10)),
        Binding("number_sign", "assign_to", "Assign to commit n"),
        Binding("x,0", "unassign", "Unassign"),
        Binding("c", "create", "New commit"),
        Binding("C", "clear_commits", "Clear commits", show=False),
        Binding("O", "restore_commits", "Original commits", show=False),
        Binding("tab", "cycle(1)", "Next commit", priority=True),
        Binding("shift+tab", "cycle(-1)", "Previous commit", show=False, priority=True),
        Binding("J", "move_commit(1)", "Move commit later", show=False),
        Binding("K", "move_commit(-1)", "Move commit earlier", show=False),
        Binding("r", "reword", "Reword", show=False),
        Binding("A", "author", "Set author", show=False),
        Binding("D", "delete", "Delete commit", show=False),
        Binding("e", "edit", "Edit file"),
        Binding("plus", "edit_other", "Edit another file", show=False),
        Binding("E", "drop_edit", "Drop edit", show=False),
        Binding("w", "write", "Write"),
        Binding("q", "quit_recommit", "Quit"),
        Binding("question_mark", "help", "Help"),
        Binding("R", "reload", "Reload", show=False),
        Binding("ctrl+z", "app.suspend_process", "Suspend", show=False),
    ]

    def __init__(self, board: Board, terminal: Terminal) -> None:
        super().__init__()
        self.board = board
        self.terminal = terminal

    @property
    def session(self) -> Session:
        return self.board.session

    def compose(self) -> ComposeResult:
        yield Static(KEYS, id="keys")
        with Horizontal():
            with VerticalScroll(id="sidebar"):
                yield Sidebar(id="commits")
            yield DiffView(self.board)
        yield Static(id="status")

    def on_mount(self) -> None:
        self.query_one(DiffView).focus()
        self.update()
        # After ctrl+z (or the editor), the user may have changed files.
        self.app.app_resume_signal.subscribe(self, lambda _: self.call_later(self.action_reload, True))

    def action_reload(self, quiet: bool = False, *, changed_message: str | None = None) -> None:
        rows = self.board.rows
        dropped = self.board.reload(quiet)
        if self.board.rows is not rows:
            self.query_one(DiffView).rebuild()
            if changed_message is not None:
                if dropped:
                    self.app.push_screen(ReloadScreen("\n\n".join([changed_message, "\n".join(dropped)])))
                self.board.message = changed_message
        self._do()

    def update(self) -> None:
        self.query_one(DiffView).update_rows()
        self.query_one(Sidebar).show(self._sidebar())
        self.query_one("#status", Static).update(self.board.message)

    def _sidebar(self) -> SidebarText:
        """The sidebar's sections, in order."""
        out = SidebarText()
        self._new_commits_section(out)
        self._rebuilding_section(out)
        self._unassigned_section(out)
        self._files_section(out)
        return out

    def _new_commits_section(self, out: SidebarText) -> None:
        out.heading("New commits")
        conflicts = {(c.commit.key, c.path) for c in self.session.conflicts()}
        for number, commit in enumerate(self.board.plan.commits, start=1):
            target: Target = ("commit", commit.key)
            active = commit.key == self.board.active
            out.add("▶ " if active else "  ", target=target)
            out.add(f"{number} ", commit_style(number), target)
            out.add(commit.subject or "(no message)", "reverse" if active else "", target)
            out.add(f"  {self.board.plan.count(commit.key)}\n", "dim", target)
            if commit.author is not None and commit.author.email != self.session.me().email:
                out.add(f"    by {commit.author.name}\n", "dim", target)
            for path in commit.edits:
                out.add(f"    ✎ {path}\n", "yellow", target)
            for key, path in sorted(conflicts):
                if key == commit.key:
                    out.add(f"    ! conflict in {path}\n", "bold red", target)
        if not self.board.plan.commits:
            out.add("  (none yet: press c)\n", "dim")

    def _unassigned_section(self, out: SidebarText) -> None:
        unassigned = len(self.session.unassigned())
        out.heading(f"Unassigned: {unassigned}", "bold" if unassigned else "dim")

    def _rebuilding_section(self, out: SidebarText) -> None:
        """Which commits writing replaces, which the new commits may no longer
        show (e.g. after C)."""
        out.heading(self.session.describe_range(), "dim")
        if self.session.uncommitted:
            out.add("and the uncommitted changes\n", "dim")
        if left_out := self.session.untracked_left_out():
            out.add(show.untracked_note(len(left_out), "run") + "\n", "dim")

    def _files_section(self, out: SidebarText) -> None:
        out.heading("Files (click one to go to it)")
        assigned = self.board.plan.assignment
        for index, diff in enumerate(self.session.files):
            target: Target = ("file", index)
            done = sum(1 for i in diff.ids if i in assigned)
            style = "dim" if done == len(diff.ids) else ""
            out.add(f"  {diff.status} ", style, target)
            out.add(diff.path, style, target)
            out.add(f"  {done}/{len(diff.ids)}\n", "dim", target)

    def on_diff_view_clicked(self, message: DiffView.Clicked) -> None:
        """Move to the row clicked; with shift, select the rows from the
        cursor to it."""
        if message.shift:
            if self.board.anchor is None:
                self.board.anchor = self.board.cursor
        else:
            self.board.anchor = None
        self.board.goto(message.index)
        self._do()

    def on_sidebar_chosen(self, message: Sidebar.Chosen) -> None:
        assert message.target is not None
        kind, value = message.target
        if kind == "commit":
            self.board.active = value
            self._do(f"Commit {self.board.number(value)} is active.")
        else:
            self.board.goto_file(value)
            self._do()

    def _do(self, message: str | None = None) -> None:
        if message is not None:
            self.board.message = message
        self.update()

    # Moving.

    def action_move(self, delta: int) -> None:
        self.board.move(delta)
        self._do()

    def action_page(self, direction: int) -> None:
        self.board.move(direction * max(1, self.query_one(DiffView).size.height - 1))
        self._do()

    def action_top(self) -> None:
        self.board.goto(0)
        self._do()

    def action_bottom(self) -> None:
        self.board.goto(len(self.board.rows) - 1)
        self._do()

    def action_jump(self, kind: str, direction: int) -> None:
        self.board.jump("file" if kind == "file" else "hunk", direction)
        self._do()

    def action_next_unassigned(self) -> None:
        self.board.next_unassigned()
        self._do()

    def action_scroll_sideways(self, direction: int) -> None:
        view = self.query_one(DiffView)
        view.scroll_to(x=max(0, view.scroll_x + direction * 8), animate=False, immediate=True)

    def action_range(self) -> None:
        self.board.toggle_range()
        self._do()

    def action_clear(self) -> None:
        self.board.anchor = None
        self._do()

    # Assigning.

    def action_assign(self) -> None:
        if self.board.active is None:
            self._do("There's no commit to assign to yet; press c to create one.")
            return
        self.board.assign(self.board.active)
        self._do()

    def action_assign_enclosing(self, kind: str) -> None:
        if self.board.active is None:
            self._do("There's no commit to assign to yet; press c to create one.")
            return
        self.board.assign_enclosing("file" if kind == "file" else "hunk", self.board.active)
        self._do()

    def action_assign_number(self, number: int) -> None:
        self.board.assign_number(number)
        self._do()

    def action_assign_to(self) -> None:
        """Ask for a commit's number, and assign to it (for when there are
        more than 9)."""

        def answered(number: str | None) -> None:
            if number and number.strip().isdigit():
                self.board.assign_number(int(number))
            elif number is not None:
                self.board.message = f"{number!r} isn't a commit's number."
            self._do()

        self.app.push_screen(InputScreen("Assign to commit number (Esc to cancel):"), answered)

    def action_unassign(self) -> None:
        self.board.assign(None)
        self._do()

    # Commits.

    def action_create(self) -> None:
        def created(message: str | None) -> None:
            if message and message.strip():
                self.board.add_commit(message.strip())
            self._do()

        self.app.push_screen(InputScreen("Message for the new commit (Esc to cancel):"), created)

    def action_cycle(self, delta: int) -> None:
        self.board.cycle_active(delta)
        self._do()

    def action_move_commit(self, delta: int) -> None:
        try:
            self.board.move_active(delta)
        except RecommitError as e:
            self.board.message = str(e)
        self._do()

    def action_delete(self) -> None:
        try:
            commit = self.board.active_commit()
        except RecommitError as e:
            self._do(str(e))
            return
        number = self.board.number(commit.key)
        lost = " and its edits are lost" if commit.edits else ""
        self._confirm(
            f"Delete commit {number} ({commit.subject})? Its changes become unassigned{lost}.",
            self.board.remove_active,
        )

    def _confirm(self, question: str, then: Callable[[], None]) -> None:
        """Ask question, and if the user says yes, do then."""

        def answered(yes: bool | None) -> None:
            if yes:
                then()
                self._do()

        self.app.push_screen(ConfirmScreen(question), answered)

    def action_author(self) -> None:
        try:
            commit = self.board.active_commit()
        except RecommitError as e:
            self._do(str(e))
            return
        number = self.board.number(commit.key)

        def answered(author: str | None) -> None:
            if author is not None:
                self.board.run(commands.SetAuthor(number, author.strip() or None))
            self._do()

        current = f" (now {commit.author or self.session.me()})"
        self.app.push_screen(
            InputScreen(f"Author of commit {number}{current}, as Name <email>; empty for you:"), answered
        )

    def action_reword(self) -> None:
        try:
            commit = self.board.active_commit()
            cleanup = editor.MessageCleanup(self.session.git, commit.message)
            saved = self.terminal.edit(self.app, cleanup.template().encode(), "COMMIT_EDITMSG")
            message = cleanup.clean(saved.decode(errors="replace"))
        except RecommitError as e:
            self._do(str(e))
            return
        self.board.reword_active(message)
        self._do()

    def action_edit_other(self) -> None:
        """Ask for a file the range doesn't change, then edit it, as e does."""

        def answered(path: str | None) -> None:
            if not path or not path.strip():
                self._do()
                return
            try:
                self.board.active_commit()
                diff = self.board.add_file(path.strip())
            except RecommitError as e:
                self._do(str(e))
                return
            self.query_one(DiffView).rebuild()
            self._edit(diff)

        self.app.push_screen(InputScreen("Edit which file in the active commit? (Esc to cancel)"), answered)

    def action_edit(self) -> None:
        self._edit(self.board.current_file())

    def _edit(self, diff: FileDiff) -> None:
        try:
            commit = self.board.active_commit()
            if diff.binary:
                self._replace(diff)
                return
            content = self.terminal.edit(self.app, self.session.edit_text(commit.key, diff), diff.path)
        except RecommitError as e:
            self._do(str(e))
            return
        self.board.edit_active(diff.path, content)
        self._do()

    def _replace(self, diff: FileDiff) -> None:
        def answered(replacement: str | None) -> None:
            if replacement and replacement.strip():
                self.board.replace_active(diff.path, replacement)
            self._do()

        prompt = f"Replace {diff.path} in the active commit with which file? (Esc to cancel)"
        self.app.push_screen(InputScreen(prompt), answered)

    def action_drop_edit(self) -> None:
        path = self.board.current_file().path
        try:
            commit = self.board.active_commit()
        except RecommitError as e:
            self._do(str(e))
            return
        number = self.board.number(commit.key)
        if path not in commit.edits:
            self._do(f"Commit {number} doesn't edit {path}.")
            return
        self._confirm(f"Drop commit {number}'s edit of {path}? It can't be undone.", lambda: self.board.drop_edit(path))

    # Finishing.

    def action_write(self) -> None:
        if problem := writing.blocker(self.session):
            self._do(problem[0].upper() + problem[1:] + ".")
            return
        lines = [writing.question(self.session)]
        if warnings := writing.warnings(self.session):
            lines.append("\nWarnings (A sets a commit's author):")
            lines.extend(f"  {warning}" for warning in warnings)

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._build()

        self.app.push_screen(ConfirmScreen("\n".join(lines)), confirmed)

    def _build(self) -> None:
        def run_validation(commits: list[tuple[str, str]]) -> validate.Outcome:
            return self.terminal.validate(self.app, commits)

        try:
            if self.session.head_moved():
                self.action_reload(changed_message="HEAD moved. Check the reloaded changes, then write again.")
                return
            shas, outcome = writing.build(
                self.session, run_validation if self.terminal.validate_command is not None else None
            )
        except RecommitError as e:
            self._do(str(e))
            return
        if outcome.action == "edit":
            assert outcome.index is not None
            self.board.active = self.board.plan.commits[outcome.index].key
            self._do(f"Commit {outcome.index + 1} failed validation; change it, then press w to try again.")
        elif outcome.action == "abort":
            self.app.exit(None)
        else:
            self.app.exit(shas)

    def action_clear_commits(self) -> None:
        self._confirm("Clear every commit, to start from scratch?", self.board.clear)

    def action_restore_commits(self) -> None:
        self._confirm(
            "Go back to the commits being rebuilt, as they were at the start? Changes to them are lost.",
            self.board.restore,
        )

    def action_quit_recommit(self) -> None:
        def confirmed(yes: bool | None) -> None:
            if yes:
                self.app.exit(None)

        self.app.push_screen(ConfirmScreen("Quit without changing any commits?"), confirmed)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())


class RecommitApp(App[list[str] | None]):
    """Returns the new commits' ids when the user writes them, or None."""

    TITLE = "git recommit"
    CSS = """
    #sidebar { width: 44; border-right: solid $panel-lighten-2; }
    #commits { padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    DiffView { width: 1fr; }
    #keys { height: auto; padding: 0 1; background: $panel; }
    #status { height: auto; max-height: 3; padding: 0 1; background: $panel; }
    .dialog {
        width: 80%; max-width: 100; height: auto; max-height: 90%;
        padding: 1 2; border: thick $accent; background: $surface;
    }
    InputScreen, ConfirmScreen, HelpScreen, ReloadScreen { align: center middle; }
    """

    def __init__(self, session: Session, terminal: Terminal) -> None:
        super().__init__()
        self.board = Board(session)
        self.terminal = terminal

    def on_mount(self) -> None:
        self.push_screen(BoardScreen(self.board, self.terminal))
