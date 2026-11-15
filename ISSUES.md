# Known issues

Most important first. "Confirmed" means it was reproduced; "suspected" means
it was found by reading the code and still needs checking.

## Starting from heavily overlapping commits takes seconds

With 60 files of 1,500 lines, each rewritten by all of 15 commits (so every
commit needs an edit for every file, 900 in all), starting from the old
commits takes a few seconds, though files are now reproduced in parallel:
most of it is one `git merge-file` per carried edit. Merging in-process would
be faster, but would have to agree with git's merges; or the TUI could open
first, and show progress.

## Merges can't be rebuilt

The range can't include merge commits: the new commits are one line of
history, so there's nowhere for a merge's other parent to go.

## Renames show as a deletion and an addition

The diff is loaded with `--no-renames`, so a renamed file is a deleted file and
an added one, and assigning the rename to one commit means assigning both.

## Edits are whole files, and text only

`e` edits a whole file, not a hunk or a line, and binary files, symlinks and
submodules can't be edited.
