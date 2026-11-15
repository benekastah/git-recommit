# Known issues

Most important first. "Confirmed" means it was reproduced; "suspected" means
it was found by reading the code and still needs checking.

## Starting from heavily overlapping commits takes seconds

With 60 files of 1,500 lines, each rewritten by all of 15 commits (so every
commit needs an edit for every file, 900 in all), starting from the old
commits takes about 15 seconds. Little of it is `git merge-file` (about 2.5
seconds of 840 merges); most is Python. For each old commit,
`oldcommits._reproduce` replays the file through every earlier commit from
scratch (`Session.states` with `upto`), so the work grows with the square of
the number of commits, and each replay rebuilds the file with `diff.apply` for
every commit (16,200 times here). `FileDiff.ids` rebuilds its list each time
it's read, which `file_states` does in its loop. Since that work holds the
GIL, reproducing files in parallel barely helps. Going through each file once,
keeping its state as it goes, and caching the ids should fix it.

## An uncommitted mode change goes to the last commit to change the file

Starting from the old commits gives a text file's change to itself (its mode)
to the last commit to change the file, even if the mode changed only in the
uncommitted changes, which so get committed. It should be unassigned then, as
an uncommitted rename is, or a file that changes as a whole.

## Edits are whole files, and text only

`e` edits a whole file, not a hunk or a line, and binary files, symlinks and
submodules can't be edited.
