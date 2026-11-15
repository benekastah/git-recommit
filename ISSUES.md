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

## A commit can't take a file the range doesn't change

`e` edits a file as it is in the active commit, but only files in the diff
have a row to press it on. So there's no way to give a new commit a file the
range doesn't touch: a change to a file that's otherwise the same at both
ends (e.g. a temporary stub), or a new file that isn't in the range at all.

It also means starting from the old commits can't reproduce a file that ends
up as it started (e.g. added by one commit and removed by a later one): it
isn't in the diff, so no commit has it. Edits also can't delete a file, so a
file an old commit deleted and a later one brought back can't be reproduced
either.

Planned: a key that asks for a path, then opens that file as it is in the
active commit (empty if it isn't there), like `e`. The file then needs a place
in the diff panel and the sidebar's files even though it has no changes, so
its edits can be seen and dropped. Starting from the old commits would add
the files they change, too.

## The range must end at HEAD

Only the commits up to HEAD can be rebuilt. Rebuilding commits further down,
and replaying the ones above on the result, isn't supported.

## Merges can't be rebuilt

The range can't include merge commits: the new commits are one line of
history, so there's nowhere for a merge's other parent to go.

## Renames show as a deletion and an addition

The diff is loaded with `--no-renames`, so a renamed file is a deleted file and
an added one, and assigning the rename to one commit means assigning both.

## Edits are whole files, and text only

`e` edits a whole file, not a hunk or a line, and binary files, symlinks and
submodules can't be edited.
