import os
import random

import pytest

from git_recommit.diff import FileDiff, apply, join_lines, load_diff, split_lines
from tests.helpers import Repo


def load(repo: Repo, base: str = "HEAD~", tip: str = "HEAD") -> dict[str, FileDiff]:
    client = repo.client
    return {d.path: d for d in load_diff(client, client.run("rev-parse", base), client.run("rev-parse", tip))}


def test_split_and_join_lines_keep_line_endings() -> None:
    assert split_lines(b"a\nb\r\nc") == [b"a\n", b"b\r\n", b"c"]
    assert split_lines(b"a\n") == [b"a\n"]
    assert split_lines(b"") == []
    # A line that lost its place at the end of the file gets a line ending.
    assert join_lines([b"a", b"b\n"]) == b"a\nb\n"
    assert join_lines([b"a\n", b"b"]) == b"a\nb"


def test_each_added_and_removed_line_is_a_change(repo: Repo) -> None:
    repo.commit("base", f="1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n")
    repo.commit("change", f="1\ntwo\n3\n4\n5\n6\n7\n8\n9\n10\n11\n")
    f = load(repo)["f"]
    assert f.status == "M"
    assert [(line.kind, line.text) for h in f.hunks for line in h.lines if line.id is not None] == [
        ("-", b"2\n"),
        ("+", b"two\n"),
        ("+", b"11\n"),
    ]
    assert f.meta_id is None
    assert len(f.ids) == 3


def test_applying_some_changes(repo: Repo) -> None:
    repo.commit("base", f="1\n2\n3\n")
    repo.commit("change", f="1\ntwo\n3\nfour\n")
    f = load(repo)["f"]
    remove_2, add_two, add_four = f.ids
    assert apply(f, set()) == b"1\n2\n3\n"
    assert apply(f, {remove_2, add_two, add_four}) == b"1\ntwo\n3\nfour\n"
    assert apply(f, {add_four}) == b"1\n2\n3\nfour\n"
    # Both the old and the new line, old first.
    assert apply(f, {add_two}) == b"1\n2\ntwo\n3\n"
    assert apply(f, {remove_2}) == b"1\n3\n"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"a\nb\nc", b"a\nb\nc\nd\n"),
        (b"a\nb\nc\n", b"a\nb"),
        (b"x\r\ny\r\n", b"x\r\nz\r\ny\r\n"),
        (b"", b"new\nfile\n"),
        (b"\n".join(b"%d" % i for i in range(100)), b"\n".join(b"%d" % (i * 3 % 101) for i in range(80))),
    ],
)
def test_applying_every_change_gives_the_new_file(repo: Repo, old: bytes, new: bytes) -> None:
    repo.commit("base", f=old)
    repo.commit("change", f=new)
    f = load(repo)["f"]
    assert apply(f, set(f.ids)) == new
    assert apply(f, set()) == old
    # Any subset makes something, without failing.
    rng = random.Random(0)
    for _ in range(20):
        apply(f, {i for i in f.ids if rng.random() < 0.5})


def test_added_and_deleted_files(repo: Repo) -> None:
    repo.commit("base", gone="bye\n", empty_gone="")
    repo.commit("change", new="hi\n", empty_new="", gone=None, empty_gone=None)
    # Else git sees empty_gone renamed to empty_new, as they're the same.
    repo.git("config", "diff.renames", "false")
    files = load(repo)
    assert files["new"].status == "A" and files["new"].meta_id is None and len(files["new"].ids) == 1
    assert files["gone"].status == "D" and len(files["gone"].ids) == 1
    # An empty file has no lines, so adding or deleting it is one change.
    for name in ("empty_new", "empty_gone"):
        assert files[name].hunks == [] and files[name].ids == [files[name].meta_id]


def test_binary_files_and_modes_change_as_a_whole(repo: Repo) -> None:
    repo.commit("base", data=b"\0\1\2", script="echo hi\n")
    repo.write("data", b"\0\3\4")
    repo.write("script", "echo hello\n")
    repo.executable("script")
    repo.commit("change")
    files = load(repo)
    assert files["data"].binary and files["data"].whole and files["data"].ids == [files["data"].meta_id]
    script = files["script"]
    assert not script.whole
    assert script.old_mode == "100644" and script.new_mode == "100755"
    assert len(script.ids) == 3 and script.meta_id in script.ids


def test_paths_with_spaces_and_unicode(repo: Repo) -> None:
    repo.commit("change", **{"dir with space/naïve.txt": "x\n"})
    assert list(load(repo)) == ["dir with space/naïve.txt"]


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_file_that_becomes_a_symlink_changes_as_a_whole(repo: Repo) -> None:
    repo.commit("base", link="a file\n", after="x\n")
    (repo.path / "link").unlink()
    (repo.path / "link").symlink_to("after")
    repo.commit("change", after="y\n")
    files = load(repo)
    assert list(files) == ["after", "link"]
    link = files["link"]
    assert (link.status, link.old_mode, link.new_mode) == ("T", "100644", "120000")
    assert link.whole and link.ids == [link.meta_id]
    assert len(files["after"].ids) == 2


LINES = "".join(f"line {i}\n" for i in range(1, 21))


def test_a_renamed_file_is_one_file_with_a_change_to_itself(repo: Repo) -> None:
    repo.commit("base", old=LINES)
    repo.commit("rename", old=None, new=LINES.replace("line 3\n", "three\n"))
    files = load(repo)
    assert list(files) == ["new"]
    new = files["new"]
    assert new.status == "R" and new.old_path == "old" and not new.whole
    assert new.describe_meta() == "renamed from old"
    # The rename is a change of its own, apart from the lines.
    assert new.meta_id in new.ids
    assert [(line.kind, line.text) for h in new.hunks for line in h.lines if line.id is not None] == [
        ("-", b"line 3\n"),
        ("+", b"three\n"),
    ]
    assert apply(new, set()) == LINES.encode()


def test_a_renamed_binary_file_changes_as_a_whole(repo: Repo) -> None:
    repo.commit("base", old=b"\0" + LINES.encode())
    repo.commit("rename", old=None, new=b"\0" + LINES.encode())
    new = load(repo)["new"]
    assert new.status == "R" and new.old_path == "old" and new.whole
    assert new.ids == [new.meta_id]
    assert new.describe_meta() == "binary, renamed from old"


def test_renames_are_found_unless_diff_renames_is_off(repo: Repo) -> None:
    repo.commit("base", old=LINES)
    repo.commit("rename", old=None, new=LINES)
    assert {path: f.status for path, f in load(repo).items()} == {"new": "R"}
    repo.git("config", "diff.renames", "false")
    assert {path: f.status for path, f in load(repo).items()} == {"new": "A", "old": "D"}


def test_a_renamed_submodule_changes_as_a_whole(repo: Repo) -> None:
    sha = repo.git("rev-parse", "HEAD")
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
    repo.git("commit", "--quiet", "-m", "base")
    repo.git("update-index", "--force-remove", "sub")
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{sha},sub2")
    repo.git("commit", "--quiet", "-m", "rename")
    sub2 = load(repo)["sub2"]
    assert sub2.status == "R" and sub2.whole and sub2.ids == [sub2.meta_id]
    assert sub2.describe_meta() == "renamed from sub"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_renamed_symlink_says_so(repo: Repo) -> None:
    (repo.path / "link").symlink_to("README")
    repo.commit("base")
    (repo.path / "link").rename(repo.path / "link2")
    repo.commit("rename")
    link2 = load(repo)["link2"]
    assert link2.status == "R" and link2.whole and link2.describe_meta() == "renamed from link"


def test_a_rename_and_a_mode_change_are_one_change(repo: Repo) -> None:
    repo.commit("base", old=LINES)
    repo.git("mv", "old", "new")
    repo.executable("new")
    repo.commit("rename")
    new = load(repo)["new"]
    assert new.ids == [new.meta_id]
    assert new.describe_meta() == "renamed from old, mode 100644 -> 100755"


def test_a_renamed_file_is_binary_if_its_attributes_say_so(repo: Repo) -> None:
    repo.commit("base", **{".gitattributes": "*.dat binary\n", "old.dat": LINES})
    repo.commit("rename", **{"old.dat": None, "new.dat": LINES})
    new = load(repo)["new.dat"]
    assert new.status == "R" and new.binary and new.whole


def test_a_file_with_nuls_its_attributes_say_is_text_is_split_into_lines(repo: Repo) -> None:
    base: dict[str, str | bytes] = {".gitattributes": "*.txt diff\n", "nuls.txt": b"a\0b\nkeep\n", "after": LINES}
    repo.commit("base", **base)
    change: dict[str, str | bytes] = {"nuls.txt": b"a\0c\nkeep\n", "after": LINES + "more\n"}
    repo.commit("change", **change)
    files = load(repo)
    nul = files["nuls.txt"]
    assert not nul.binary
    assert [(line.kind, line.text) for line in nul.hunks[0].lines if line.id is not None] == [
        ("-", b"a\0b\n"),
        ("+", b"a\0c\n"),
    ]
    # And the files after it are read as they are.
    assert [line.text for line in files["after"].hunks[0].lines if line.id is not None] == [b"more\n"]


def test_a_binary_files_mode_change_says_so(repo: Repo) -> None:
    repo.commit("base", data=b"\0bin")
    repo.executable("data")
    repo.commit("mode")
    data = load(repo)["data"]
    assert data.binary and data.ids == [data.meta_id]
    assert data.describe_meta() == "binary, mode 100644 -> 100755"


def test_an_empty_diff_renames_is_off_as_git_reads_it(repo: Repo) -> None:
    repo.commit("base", old=LINES)
    repo.commit("rename", old=None, new=LINES)
    repo.git("config", "diff.renames", "")
    assert {path: f.status for path, f in load(repo).items()} == {"new": "A", "old": "D"}
    repo.git("config", "diff.renames", "copies")
    assert {path: f.status for path, f in load(repo).items()} == {"new": "R"}
