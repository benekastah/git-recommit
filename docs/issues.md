# Issues

What's wrong with what's built: bugs, and gaps against the rules in `AGENTS.md` and
`PROJECT_AGENTS.md`, most severe first. New work is in [roadmap.md](roadmap.md). When a change
fixes one, remove it in the same change; when a change leaves something wrong, add it.

"Confirmed" means it was reproduced; "suspected" means it was found by reading the code and
still needs checking.

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

## A submodule the config ignores can move in the uncommitted changes

Confirmed. With `submodule.<name>.ignore = all` (or `diff.ignoreSubmodules`)
in the git config, `git status` doesn't show a submodule checked out at
another commit than HEAD has, and so neither does git recommit, unless
something else is uncommitted: then `revisions.worktree_commit`'s `git add`
takes the submodule's move too, and it's an (unassigned) uncommitted change.
(`ignore = all` in `.gitmodules` keeps `git add` from taking it.) The commit
of the working tree should take only what `git status` shows.

## An uncommitted mode change to a file an old commit added or renamed is committed with it

A file's rename (or adding it) and its mode are one change to the file itself,
which can't be split. When an old commit renamed or added the file and the
uncommitted changes (and not the range's commits) changed its mode, starting
from the old commits gives the change to the commit that made it, so it
commits the mode too. Leaving it uncommitted would leave that commit without
the rename or the file.

## A rename spread over several old commits isn't reproduced

A file's rename is one change (one `FileDiff` covers both paths), which goes to
one commit: the first whose tree has the new path and not the old. So when
one old commit copies a file and a later one deletes the old, or a file is
renamed back and forth, the commits in between are rebuilt without the file
(or empty), and writing says nothing about it.

## On Windows, the TUI runs the editor with cmd.exe

git runs editors with its own sh, even on Windows, but `e` and `r` run
`git var GIT_EDITOR` through Python's shell, which is cmd.exe there. So an
editor command written for sh (e.g. `'C:/Program Files/Vim/vim.exe'`, in
single quotes) fails, as does git's fallback, `vi`, which isn't on cmd.exe's
PATH. Running it as git does (e.g. through a `!` alias, which also gets
git's PATH) would fix it.

## On Windows, a name that isn't UTF-8 can't be passed to git

Arguments on Windows are Unicode, so a path that isn't UTF-8 (from a
repository made elsewhere) can't be given to git as one, e.g. to git blame
when starting from the old commits. Writing such a name into a commit (by
the index, through stdin) works.

## Signing and the git wrapper aren't tested on Windows

`test_signs_commits_when_git_is_set_to` (`tests/test_session.py`) and
`test_every_git_process_is_git_recommit_git` (`tests/test_git.py`) fake gpg
and git with shell scripts, so they're skipped on Windows, and nothing tests
either there. `AGENTS.md` asks for a test of its own on the other systems: a
fake written in Python, with a launcher Windows can run.
