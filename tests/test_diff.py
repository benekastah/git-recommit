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
    (repo.path / "script").chmod(0o755)
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
