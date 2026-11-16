import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from textual.app import App
from textual.pilot import Pilot

from git_recommit import validate
from git_recommit.errors import RecommitError
from git_recommit.session import Session
from git_recommit.tui import DiffView, RecommitApp, Sidebar, Terminal
from tests.helpers import Repo


class FakeTerminal(Terminal):
    """Edits with edit_with instead of an editor, and validates with outcomes."""

    def __init__(self, repo: Repo, edit_with: Callable[[bytes], bytes] = lambda b: b) -> None:
        super().__init__(repo.client, None)
        self.edit_with = edit_with
        self.outcomes: list[validate.Outcome] = []
        self.validated: list[list[tuple[str, str]]] = []

    def edit(self, app: App[object], content: bytes, filename: str) -> bytes:
        return self.edit_with(content)

    def validate(self, app: App[object], commits: list[tuple[str, str]]) -> validate.Outcome:
        self.validated.append(commits)
        return self.outcomes.pop(0)


def run(session: Session, terminal: Terminal, script: Callable[[Pilot[Any]], Coroutine[Any, Any, None]]) -> Any:
    app = RecommitApp(session, terminal)

    async def go() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await script(pilot)
            await pilot.pause()

    asyncio.run(go())
    return app.return_value


def two_files(repo: Repo) -> Session:
    repo.commit("wip", a="a\n")
    repo.commit("wip 2", b="b\n")
    return repo.session("HEAD~2")


async def create(pilot: Pilot[Any], message: str) -> None:
    await pilot.press("c")
    await pilot.press(*message)
    await pilot.press("enter")


def test_assign_files_and_write(repo: Repo) -> None:
    session = two_files(repo)

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "add-a")
        await pilot.press("f")  # a, and on to b
        await create(pilot, "add-b")
        await pilot.press("f", "w", "y")

    shas = run(session, FakeTerminal(repo), script)
    assert len(shas) == 2
    assert repo.show(shas[0], "a") == "a\n" and repo.show(shas[0], "b") is None
    assert repo.show(shas[1], "b") == "b\n"
    assert [c.message for c in session.plan.commits] == ["add-a", "add-b"]


def test_choosing_the_active_commit(repo: Repo) -> None:
    session = two_files(repo)

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "one")
        await create(pilot, "two")
        await pilot.press("shift+tab", "f")
        await pilot.click("#commits", offset=(4, 2))  # the line under "New commits" and "one"
        await pilot.press("f")

    run(session, FakeTerminal(repo), script)
    one, two = (c.key for c in session.plan.commits)
    assert sorted(session.plan.assignment.items()) == [(0, one), (1, two)]


def test_jumping_to_a_hunk_scrolls_it_to_the_top(repo: Repo) -> None:
    # Ten hunks, too many to fit on the screen.
    repo.commit("base", f="".join(f"{i}\n" for i in range(200)))
    repo.commit("change", f="".join(f"{i}{' changed' if i % 20 == 10 else ''}\n" for i in range(200)))
    session = repo.session("HEAD~")

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        hunks = [i for i, row in enumerate(pilot.app.board.rows) if row.kind == "hunk"]
        view = pilot.app.screen.query_one("DiffView")
        await pilot.press("right_curly_bracket")
        assert view.scroll_offset.y == 0  # the file's name, then its first hunk
        await pilot.press("right_curly_bracket", "right_curly_bracket")
        await pilot.pause()
        assert view.scroll_offset.y == hunks[2]
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)


def test_resuming_reloads_the_changes(repo: Repo) -> None:
    session = two_files(repo)

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        await create(pilot, "one")
        await pilot.press("f")
        # As if after ctrl+z, the user changes a file, then brings it back.
        repo.write("b", "b\nmore\n")
        pilot.app.app_resume_signal.publish(pilot.app)
        await pilot.pause()
        lines = [row.line.text for row in pilot.app.board.rows if row.line]
        assert lines == [b"a\n", b"b\n", b"more\n"]
        assert "Reloaded" in str(pilot.app.screen.query_one("#status").render())
        await pilot.press("f", "w", "y")

    shas = run(session, FakeTerminal(repo), script)
    assert repo.show(shas[0], "b") == "b\nmore\n"


def test_clicking_a_hunk_selects_it(repo: Repo) -> None:
    repo.commit("base", f="".join(f"{i}\n" for i in range(20)))
    repo.commit("change", f="".join(f"{i}{' changed' if i in (2, 15) else ''}\n" for i in range(20)))
    session = repo.session("HEAD~")

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        await create(pilot, "one")
        board = pilot.app.board
        second = [i for i, row in enumerate(board.rows) if row.kind == "hunk"][1]
        await pilot.click("DiffView", offset=(10, second))
        assert board.cursor == second
        assert board.selection == (second, board.extent(second))
        await pilot.press("space")
        # Clicking a line moves to it; shift-click selects from there to the row clicked.
        await pilot.click("DiffView", offset=(10, 4))
        assert board.cursor == 4 and board.selection == (4, 4)
        await pilot.click("DiffView", offset=(10, 2))
        await pilot.click("DiffView", offset=(10, 3), shift=True)
        assert board.selection == (2, 3)
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)
    texts = {line.id: line.kind + line.text.decode() for h in session.files[0].hunks for line in h.lines}
    assert sorted(texts[i] for i in session.plan.assignment) == ["+15 changed\n", "-15\n"]


def test_clicking_a_file_in_the_sidebar_goes_to_it(repo: Repo) -> None:
    # A subject too long for the sidebar, which mustn't wrap onto more lines.
    long = "a commit subject that is much too long to fit in the sidebar on one line"
    repo.commit(long, **{f"f{i}": "".join(f"{j}\n" for j in range(30)) for i in range(5)})
    session = repo.session("HEAD~")

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        sidebar = pilot.app.screen.query_one("Sidebar")
        assert isinstance(sidebar, Sidebar)
        assert sidebar.size.height == len(sidebar.targets)
        line = sidebar.targets.index(("file", 3))
        await pilot.click("Sidebar", offset=(4, line))
        await pilot.pause()
        board = pilot.app.board
        row = board.rows[board.cursor]
        assert row.kind == "file" and session.files[row.file].path == "f3"
        assert pilot.app.screen.query_one("DiffView").scroll_offset.y == board.cursor
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)


def test_clearing_and_restoring_the_old_commits(repo: Repo) -> None:
    session = two_files(repo)
    session.restore_old_commits()

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        sidebar = pilot.app.screen.query_one("Sidebar")
        assert "Rebuilding 2 commits after" in str(sidebar.render())
        assert pilot.app.board.active == session.plan.commits[0].key
        await pilot.press("C", "y")
        assert session.plan.commits == [] and pilot.app.board.active is None
        await pilot.press("O", "y")
        assert [c.message for c in session.plan.commits] == ["wip", "wip 2"]
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)


def test_writing_after_head_moved_reloads_first(repo: Repo) -> None:
    session = two_files(repo)
    session.restore_old_commits()

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        repo.commit("meanwhile", c="c\n")
        await pilot.press("w", "y")
        assert pilot.app.return_value is None
        status = str(pilot.app.screen.query_one("#status").render())
        assert "HEAD moved" in status
        # The new commit's change is there now, to assign before writing.
        assert [d.path for d in session.files] == ["a", "b", "c"]
        await pilot.press("q", "y")

    assert run(session, FakeTerminal(repo), script) is None


def test_deleting_a_commit_asks_first(repo: Repo) -> None:
    session = two_files(repo)
    session.restore_old_commits()

    async def script(pilot: Pilot[Any]) -> None:
        await pilot.press("D", "n")
        assert [c.message for c in session.plan.commits] == ["wip", "wip 2"]
        await pilot.press("D", "y")
        assert [c.message for c in session.plan.commits] == ["wip 2"]
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)


def test_dropping_an_edit_asks_first(repo: Repo) -> None:
    repo.commit("wip", a="real\n")
    session = repo.session("HEAD~")
    session.restore_old_commits()

    async def script(pilot: Pilot[Any]) -> None:
        await pilot.press("e")
        assert set(session.plan.commits[0].edits) == {"a"}
        await pilot.press("E", "n")
        assert set(session.plan.commits[0].edits) == {"a"}
        await pilot.press("E", "y")
        assert session.plan.commits[0].edits == {}
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo, lambda content: b"edited\n"), script)


def test_assigning_to_any_commit_by_number(repo: Repo) -> None:
    session = two_files(repo)
    for n in range(1, 13):
        session.plan.add(f"commit {n}")

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        await pilot.press("number_sign", "1", "2", "enter")
        assert pilot.app.board.active == session.plan.commits[11].key
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)
    assert set(session.plan.assignment.values()) == {session.plan.commits[11].key}


def test_the_sidebar_says_untracked_files_are_left_out(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    repo.write("new.txt", "new\n")
    session = repo.session("HEAD~")

    async def script(pilot: Pilot[Any]) -> None:
        assert "1 untracked file left out" in str(pilot.app.screen.query_one("Sidebar").render())
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)


def test_authors(repo: Repo) -> None:
    repo.commit("wip", a="a\n")
    repo.git("commit", "--quiet", "--amend", "--no-edit", "--author=Someone Else <else@example.com>")
    session = repo.session("HEAD~")
    session.restore_old_commits()

    async def script(pilot: Pilot[Any]) -> None:
        sidebar = pilot.app.screen.query_one("Sidebar")
        assert "by Someone Else" in str(sidebar.render())
        # A commit of mine taking their change: writing warns.
        await create(pilot, "mine")
        await pilot.press("f", "w")
        dialog = str(pilot.app.screen.query_one("Static").render())
        assert "commit 2 (mine) will be authored by Test <test@example.com>, but has changes by Someone Else" in dialog
        await pilot.press("n")
        # So make it theirs.
        await pilot.press("A")
        await pilot.press(*"Someone Else <else@example.com>")
        await pilot.press("enter")
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)
    assert str(session.plan.commits[1].author) == "Someone Else <else@example.com>"
    assert session.authorship_warnings() == []


def test_editing_a_file_the_range_doesnt_change(repo: Repo) -> None:
    session = two_files(repo)
    session.restore_old_commits()

    async def script(pilot: Pilot[Any]) -> None:
        assert isinstance(pilot.app, RecommitApp)
        await pilot.press("plus")
        await pilot.press(*"README", "enter")
        assert any(session.files[row.file].path == "README" for row in pilot.app.board.rows)
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo, lambda content: b"stubbed\n"), script)
    assert session.plan.commits[0].edits["README"].content == b"stubbed\n"


def test_quit_changes_nothing(repo: Repo) -> None:
    session = two_files(repo)

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "x")
        await pilot.press("q", "y")

    assert run(session, FakeTerminal(repo), script) is None


def test_writing_with_unassigned_changes_warns(repo: Repo) -> None:
    session = two_files(repo)

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "add-a")
        await pilot.press("f", "w")
        text = str(pilot.app.screen.query_one("Static").render())
        assert "1 change isn't assigned" in text and "b will be left with uncommitted changes" in text
        await pilot.press("y")

    assert len(run(session, FakeTerminal(repo), script)) == 1


def test_selecting_lines_with_V(repo: Repo) -> None:
    repo.commit("wip", a="1\n2\n3\n")
    session = repo.session("HEAD~")

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "one")
        await pilot.press("j", "j", "V", "j", "space")  # lines 1 and 2
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo), script)
    assert sorted(session.plan.assignment) == [0, 1]


def edit_file(repo: Repo, edit_with: Callable[[bytes], bytes]) -> tuple[Session, list[bytes], str]:
    """Press e on a (the only file, which the range adds) in a new commit,
    with edit_with as the editor. Return the session, what the editor
    opened, and the status line after."""
    repo.commit("wip", a="real\n")
    session = repo.session("HEAD~")
    opened: list[bytes] = []
    status: list[str] = []

    def editor(content: bytes) -> bytes:
        opened.append(content)
        return edit_with(content)

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "one")
        await pilot.press("e")
        status.append(str(pilot.app.screen.query_one("#status").render()))
        await pilot.press("q", "y")

    run(session, FakeTerminal(repo, editor), script)
    return session, opened, status[0]


def test_editing_a_file_the_commit_lacks_starts_from_its_end_state(repo: Repo) -> None:
    session, opened, _ = edit_file(repo, lambda content: b"cut down\n")
    assert opened == [b"real\n"]
    assert session.plan.commits[0].edits["a"].content == b"cut down\n"


def test_quitting_the_editor_without_saving_changes_nothing(repo: Repo) -> None:
    session, _, status = edit_file(repo, lambda content: content)
    assert session.plan.commits[0].edits == {}
    assert "unchanged" in status


def test_a_failing_editor_changes_nothing(repo: Repo) -> None:
    def fail(content: bytes) -> bytes:
        raise RecommitError("the editor failed, so nothing was changed")

    session, _, status = edit_file(repo, fail)
    assert session.plan.commits[0].edits == {}
    assert status == "the editor failed, so nothing was changed"


def test_editing_a_file_in_a_commit(repo: Repo) -> None:
    repo.commit("wip", a="real\n")
    session = repo.session("HEAD~")
    opened: list[bytes] = []
    saves = [b"stub\n", b"real\n"]

    def edit_with(content: bytes) -> bytes:
        opened.append(content)
        return saves.pop(0)

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "stub")
        await pilot.press("e")
        await create(pilot, "real")
        await pilot.press("f", "w")
        # The stub and the real thing overlap, so carrying the stub conflicts.
        assert "Resolve the conflicts first" in str(pilot.app.screen.query_one("#status").render())
        await pilot.press("k", "e", "w", "y")

    shas = run(session, FakeTerminal(repo, edit_with), script)
    assert b"<<<<<<< edit" in opened[1]
    assert repo.show(shas[0], "a") == "stub\n"
    assert repo.show(shas[1], "a") == "real\n"


def test_validation_failure_goes_back_to_edit(repo: Repo) -> None:
    session = two_files(repo)
    terminal = FakeTerminal(repo)
    terminal.validate_command = "make test"
    terminal.outcomes = [validate.Outcome("edit", 0), validate.Outcome("ok")]

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "one")
        await pilot.press("f", "f", "w", "y")
        assert pilot.app.return_value is None
        assert "failed validation" in str(pilot.app.screen.query_one("#status").render())
        await pilot.press("w", "y")

    shas = run(session, terminal, script)
    assert len(shas) == 1 and len(terminal.validated) == 2


def test_validation_abort(repo: Repo) -> None:
    session = two_files(repo)
    terminal = FakeTerminal(repo)
    terminal.validate_command = "make test"
    terminal.outcomes = [validate.Outcome("abort")]

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "one")
        await pilot.press("f", "f", "w", "y")

    assert run(session, terminal, script) is None


def test_a_rename_is_a_row_of_its_own(repo: Repo) -> None:
    repo.commit("base", old="".join(f"line {i}\n" for i in range(1, 21)))
    repo.commit("rename", old=None, new="".join(f"line {i}\n" for i in range(1, 21)).replace("line 3\n", "three\n"))
    session = repo.session("HEAD~")

    async def script(pilot: Pilot[Any]) -> None:
        texts = pilot.app.screen.query_one(DiffView)._texts  # pyright: ignore[reportPrivateUsage]
        assert texts[:2] == ["R old -> new", "  renamed from old"]
        await create(pilot, "Rename")
        await pilot.press("j", "space")  # the rename's row

    run(session, FakeTerminal(repo), script)
    (new,) = session.files
    assert list(session.plan.assignment) == [new.meta_id]


def test_e_replaces_a_binary_file_with_another(repo: Repo) -> None:
    repo.commit("base", data=b"\0one")
    repo.commit("change", data=b"\0two")
    session = repo.session("HEAD~")
    # Next to the repository, and named relative to it, where git recommit runs.
    (repo.path.parent / "stub.bin").write_bytes(b"\0stub")

    async def script(pilot: Pilot[Any]) -> None:
        await create(pilot, "Stub data")
        await pilot.press("e", *"../stub.bin", "enter")
        await create(pilot, "Not there")
        await pilot.press("e", *"../missing", "enter")
        assert "Can't read" in str(pilot.app.screen.query_one("#status").render())

    terminal = FakeTerminal(repo, edit_with=lambda _: pytest.fail("a binary file doesn't open in an editor"))
    run(session, terminal, script)
    (sha, _) = session.build()
    assert repo.git("cat-file", "blob", f"{sha}:data") == "\0stub"
