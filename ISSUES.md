# Known issues

Most important first. "Confirmed" means it was reproduced; "suspected" means
it was found by reading the code and still needs checking.

## Starting from heavily overlapping commits takes seconds

With 60 files of 1,500 lines, each rewritten by all of 15 commits (so every
commit needs an edit for every file, 900 in all), starting from the old
commits takes about 3 seconds (on 8 cores). Most of it is the 840 `git
merge-file` processes that carry the edits, which run in parallel, a file
each. Merging in-process would be faster, but would have to agree with git's
merges; or the TUI could open first, and show progress.

## Edits are whole files, and text only

`e` edits a whole file, not a hunk or a line, and binary files, symlinks and
submodules can't be edited.
