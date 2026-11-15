# Known issues

Most important first.

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

## Untracked files aren't included

The diff runs to the working tree's tracked files, as `git stash` takes them.
A file created while git recommit is suspended (`ctrl+z`), or before it
started, doesn't show up until it's `git add`ed, which is easy to miss. An
option to include untracked (but not ignored) files, or a note in the sidebar
that there are some, would help.

## Commits the user creates take their author

A commit that starts from an old one keeps its author and date, but one made
with `c` gets the current user and time, even when it takes someone else's
changes. ROADMAP.md lists ways to do better.

## The range must end at HEAD

Only the commits up to HEAD can be rebuilt. Rebuilding commits further down,
and replaying the ones above on the result, isn't supported.

## No root commits or merges

The range can't include the root commit, since there's no commit before it to
build on, or merge commits.

## Renames show as a deletion and an addition

The diff is loaded with `--no-renames`, so a renamed file is a deleted file and
an added one, and assigning the rename to one commit means assigning both.

## Edits are whole files, and text only

`e` edits a whole file, not a hunk or a line, and binary files, symlinks and
submodules can't be edited.

## Only nine commits have number keys

`1`-`9` assign to the first nine commits; later ones are reached with `tab` or
by clicking them.
