# Known issues

Most important first. "Confirmed" means it was reproduced; "suspected" means
it was found by reading the code and still needs checking.

## Starting from heavily overlapping commits takes seconds

With 60 files of 1,500 lines, each rewritten by all of 15 commits (so every
commit needs an edit for every file, 900 in all), starting from the old
commits takes about 3 seconds. Most of it is Python, which holds the GIL, so
reproducing files in parallel helps little: `oldcommits._reproduce` still
calls `Session.states` once per commit (and again in `set_edit`), and each
call groups all of the file's changes by commit again (`FileDiff.ids`
rebuilds its list each time it's read); `diff.apply` and git blame take
most of the rest. The 840 `git merge-file` processes are about half a second
of it. Going through each file once, keeping its state as it goes, would
take out the repeated calls; or the TUI could open first, and show progress.

## A replaced binary file is read and hashed again and again

A binary file's edit is read whole to tell whether it changes anything, and
hashed more than once; and the plan file keeps it by content, so each
subcommand reads and encodes it to save the plan, and hashes it again to
load it. Correct, but slow for a large file. Comparing blob ids would do.

## An uncommitted mode change goes to the last commit to change the file

Starting from the old commits gives a text file's change to itself (its mode)
to the last commit to change the file, even if the mode changed only in the
uncommitted changes, which so get committed. It should be unassigned then, as
an uncommitted rename is, or a file that changes as a whole.

## A mode that changes and changes back isn't reproduced

Starting from the old commits reproduces each file's content with edits, but
an edit can't change a file's mode (or kind, e.g. a file to a symlink). So a
file whose mode changes in one old commit and changes back in a later one
(which the range's diff doesn't show) keeps its mode in every new commit.
