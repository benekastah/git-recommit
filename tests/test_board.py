from git_recommit.board import Board
from tests.helpers import Repo


def board(repo: Repo) -> Board:
    repo.commit("base", f="1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n", g="g\n")
    repo.commit("change", f="one\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\ntwelve\n", g="G\n")
    return Board(repo.session("HEAD~"))


def kinds(b: Board) -> str:
    return "".join(
        {"file": "F", "meta": "M", "hunk": "H"}.get(row.kind, row.line.kind if row.line else "?") for row in b.rows
    )


def test_rows(repo: Repo) -> None:
    b = board(repo)
    assert kinds(b) == "FH-+   H   -+F" + "H-+"


def test_line_numbers(repo: Repo) -> None:
    b = board(repo)
    numbers = [(row.old_number, row.new_number) for row in b.rows[:7]]
    assert numbers == [(None, None), (None, None), (1, None), (None, 1), (2, 2), (3, 3), (4, 4)]
    g_lines = [(row.old_number, row.new_number) for row in b.rows if row.file == 1 and row.kind == "line"]
    assert g_lines == [(1, None), (None, 1)]


def test_jumping_by_hunk_skips_file_headers(repo: Repo) -> None:
    b = board(repo)
    hunks = [i for i, row in enumerate(b.rows) if row.kind == "hunk"]
    for expected in hunks:
        b.jump("hunk", 1)
        assert b.cursor == expected
        # A file's first hunk shows with the file's name above it.
        assert b.scroll_top == (expected - 1 if b.rows[expected - 1].kind == "file" else expected)
    for expected in reversed(hunks[:-1]):
        b.jump("hunk", -1)
        assert b.cursor == expected


def test_jumping_by_hunk_stops_at_files_without_hunks(repo: Repo) -> None:
    repo.commit("base", data=b"\0old", f="1\n")
    repo.commit("change", data=b"\0new", f="2\n")
    b = Board(repo.session("HEAD~"))
    assert kinds(b) == "FM" + "FH-+"
    b.jump("hunk", 1)
    assert b.cursor == 3  # past f's header, to its hunk
    b.jump("hunk", -1)
    assert b.cursor == 0  # the binary file, which has no hunk to stop at


def test_assigning_a_line_moves_past_it(repo: Repo) -> None:
    b = board(repo)
    b.add_commit("one")
    assert b.active is not None
    b.goto(2)
    b.assign(b.active)
    assert b.plan.assignment == {0: b.active}
    assert b.cursor == 3


def test_a_header_selects_what_it_stands_for(repo: Repo) -> None:
    b = board(repo)
    assert b.selection == (0, 12)  # f's header: all of f
    b.goto(1)
    assert b.selection == (1, 6)  # its first hunk's header: the hunk
    b.goto(2)
    assert b.selection == (2, 2)


def test_assigning_a_range(repo: Repo) -> None:
    b = board(repo)
    b.add_commit("one")
    b.goto(2)
    b.toggle_range()
    b.move(1)
    assert b.selection == (2, 3)
    b.assign(b.active)
    assert set(b.plan.assignment) == {0, 1}
    assert b.anchor is None


def test_assigning_the_enclosing_hunk_and_file(repo: Repo) -> None:
    b = board(repo)
    b.add_commit("one")
    b.goto(5)  # a context line in f's first hunk
    b.assign_enclosing("hunk", b.active)
    assert set(b.plan.assignment) == {0, 1}
    b.goto(9)
    b.assign_enclosing("file", b.active)
    assert set(b.plan.assignment) == {0, 1, 2, 3}
    assert b.rows[b.cursor].kind == "file"  # g's


def test_numbers_assign_to_that_commit(repo: Repo) -> None:
    b = board(repo)
    b.add_commit("one")
    b.add_commit("two")
    first = b.plan.commits[0].key
    b.goto(0)
    b.assign_number(1)
    assert b.active == first
    assert set(b.plan.assignment.values()) == {first}
    b.assign_number(5)
    assert b.message == "There's no commit 5."


def test_commits_are_added_after_the_active_one(repo: Repo) -> None:
    b = board(repo)
    b.add_commit("one")
    b.add_commit("three")
    b.cycle_active(-1)
    b.add_commit("two")
    assert [c.message for c in b.plan.commits] == ["one", "two", "three"]
    b.move_active(1)
    assert [c.message for c in b.plan.commits] == ["one", "three", "two"]
    b.remove_active()
    assert [c.message for c in b.plan.commits] == ["one", "three"]


def test_next_unassigned(repo: Repo) -> None:
    b = board(repo)
    b.add_commit("one")
    b.goto(0)
    b.assign_enclosing("file", b.active)
    b.goto(0)
    b.next_unassigned()
    line = b.rows[b.cursor].line
    assert line is not None and line.id == 4


def test_next_unassigned_finds_changes_that_arent_lines(repo: Repo) -> None:
    repo.commit("base", data=b"\0old", script="echo\n")
    repo.write("data", b"\0new")
    (repo.path / "script").chmod(0o755)
    repo.commit("change")
    b = Board(repo.session("HEAD~"))
    assert kinds(b) == "FM" + "FM"
    b.goto(2)
    b.next_unassigned()
    assert b.cursor == 3
    b.next_unassigned()
    assert b.cursor == 1
    b.add_commit("one")
    b.goto(0)
    b.assign(b.active)
    b.next_unassigned()
    assert b.cursor == 3 and b.message != "Every change is assigned."


def test_a_mode_change_has_a_row_of_its_own(repo: Repo) -> None:
    repo.commit("base", script="echo\n")
    repo.write("script", "echo hi\n")
    (repo.path / "script").chmod(0o755)
    repo.commit("change")
    b = Board(repo.session("HEAD~"))
    assert kinds(b) == "FMH-+"
    diff = b.session.files[0]
    assert diff.describe_meta() == "mode 100644 -> 100755"
    assert b.row_ids(1) == [diff.meta_id]
    # The file's row still stands for all of it.
    assert sorted(b.row_ids(0)) == sorted(diff.ids)
    b.add_commit("one")
    b.goto(1)
    b.assign(b.active)
    assert list(b.plan.assignment) == [diff.meta_id]
