# Known issues

Most important first. "Confirmed" means it was reproduced; "suspected" means
it was found by reading the code and still needs checking.

## The plan is lost if HEAD moved since the last reload

Confirmed. The new commits are written (`Session.finish`, called from cli.py)
after the TUI has exited. If HEAD moved and the user didn't reload (`R`, or
resuming from `ctrl+z`), `update-ref`'s check of the old value rightly fails,
but only git's error is printed: not the new commits' ids, so the work done
in the TUI can't be recovered.

Fix: check HEAD before leaving the TUI (and reload, or say so), and print the
new tip's id in any error after building.

## A file that changes kind (e.g. to a symlink) crashes loading

Confirmed. `git diff-tree -p` shows a change of kind as two parts (a deletion
and an addition) where `--raw` lists one entry, so `diff.load` fails its count
check with `AssertionError: ... listed 2 files but patched 3`. The `T`
handling in `FileDiff.whole` is never reached.

Fix: match the patch's parts to the raw entries by path rather than position
(or don't read the patch for `T` entries), and make a mismatch a
`RecommitError`, which the CLI reports, rather than an `AssertionError`.

## File names that aren't UTF-8 crash writing

Confirmed. Paths are decoded with `surrogateescape`, but `Session.build` sends
`update-index --index-info` its input through `GitClient.run(text=True)`,
which encodes strictly: a commit adding `caf\xe9.txt` raises
`UnicodeEncodeError` on writing. Nothing is written, but the TUI dies.

Fix: build the input as bytes (`os.fsencode`) and send it with `run_bytes`.

## `D` and `E` throw work away without asking

`C`, `O` and `q` ask first, but `D` (delete the active commit, with its edits)
and `E` (drop an edit) don't, and there's no undo, so a hand-made edit is lost
to one keystroke. Ask first, or add undo (see ROADMAP.md: commands as data
would give it).

## Edits on a file that leaves the diff are ignored

After a reload, an edit of a file that's no longer in the diff stays in the
commit's edits, and the sidebar still shows ✎, but building ignores it, since
it only builds files in the diff. Drop such edits on reload and say so, or
keep the file in the diff (see "A commit can't take a file the range doesn't
change").

## An unassigned new file ends up untracked

Unassigned changes are meant to be left uncommitted, but writing finishes with
a mixed `git reset`, which unstages an added file the new commits don't have,
so it becomes untracked. Untracked files aren't included, so it then vanishes
from the next `git recommit` (and from `git stash`). Keep it staged (e.g.
`git add --intent-to-add`, or reset only paths the new commits have), or at
least say so in the summary.

## Slow on big ranges with overlapping commits

Confirmed, with 60 files of 1,500 lines and 15 overlapping commits:
`restore_old_commits` took 18 s (900 edits), and runs again on `O`; and
`Session.conflicts()` took 150-230 ms, and runs on every redraw, including
each `j`/`k`.

- `_reproduce` calls `states()` (which applies every commit's changes) for
  each commit and file, and `set_edit` calls it again: compute each file's
  states once per pass.
- Cache `conflicts()` (and the sidebar's counts) until the plan changes.
- Each `merge_file` is four git processes, three of which write loose objects
  into the user's repository; `_merges` is unbounded and keyed by whole file
  contents (key it by blob id).
- `_blobs` runs `ls-tree -r` over the whole tree for each old commit (every
  file of a monorepo, N times): limit it to the diff's paths.
- `diff.load` runs a `cat-file` per file, and restoring runs a `git log` and
  two blames per file: use `cat-file --batch`, and fewer processes.

## `.` skips changes that aren't lines

Confirmed. `.` (next unassigned change) only stops on line rows, so an
unassigned binary file, mode change, symlink or empty file is never found:
with a binary file and a `chmod`, it said "Every change is assigned" with two
unassigned. It should also stop on file rows whose `meta_id` is unassigned.

## Commits aren't signed, and hooks don't run

Suspected. `commit-tree` doesn't honor `commit.gpgSign` (only `-S`), so users
who sign their commits get unsigned ones, and `commit-msg` hooks (e.g.
Gerrit's Change-Id) don't run. At least document it; pass `-S` when
`commit.gpgSign` is set.

## Paths are read as pathspecs

Suspected. `git log -- <path>` (in `restore_old_commits`) treats `[`, `*` and a
leading `:` as pathspec magic, so `a[1].txt` also matches `a1.txt`, and the
wrong commit can be taken as the last to change a file. Set
`GIT_LITERAL_PATHSPECS=1` for git recommit's git commands (or use
`:(literal)`).

## Windows: text encoding, and line endings in edits

Suspected. `GitClient.run` decodes with the locale's encoding (cp1252 on
Windows), so author names and messages read from `git log`, and messages
written with `commit-tree -F -`, can be mangled: use `encoding="utf-8",
errors="surrogateescape"`. And edits are hashed as they are (`hash-object
--stdin` applies no filters), so on Windows with `core.autocrlf`, an editor
that saves CRLF commits CRLF into a repository that stores LF: consider
`hash-object --path=<path>`, so attributes apply.

## `--edit` with `rebase.autosquash`

Suspected. If `rebase.autosquash` reorders the todo list, the commit to edit
may be a `fixup` line rather than `pick`, and `sequence_editor.mark_edit` says
it "isn't in the rebase's todo list". Match any command's line for the commit,
or pass `--no-autosquash`.

## Rewording ignores git's comment settings

`r` removes the lines that start with `#` from the message it opens
(`editor.strip_comments`), as git does when it opens an editor for a message
(e.g. `git commit --amend`). But it ignores the settings that change that:
`core.commentChar` (including `auto`, which picks a character the message
doesn't use) and `commit.cleanup` (e.g. `whitespace` or `verbatim`, which
keep `#` lines). Read them, as git does, e.g. by having git clean up the
message (`git stripspace`, with `--comment-lines` for the help text).

## Writing isn't atomic, and has side effects

- `finish` runs `update-ref`, then `git reset`; if `index.lock` is held, HEAD
  moves but the index doesn't. Nothing is lost, but it's confusing. Check for
  the lock first.
- `git status` in `worktree_commit` may rewrite the real index (refreshing
  it): use `--no-optional-locks`.
- Merging edits writes blobs into the object database (`merge_file`).
- `--validate`'s `git worktree add` runs the user's `post-checkout` hook.
- `GitClient.run` strips its output, so a message's leading blank lines and
  whitespace are lost when read back.

## Mode changes are invisible

A file row shows only `M path`, so a mode change (or a symlink's new target)
can't be seen, and a mode change can't be assigned apart from the file's lines
except by reassigning the lines afterwards. Show it on the file's row (e.g.
`M path (100644 → 100755)`), and give it a row of its own.

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

## Logic has leaked out of board.py, and session.py does too much

AGENTS.md says to keep the TUI's logic in `board.py` or the commands, where
it's tested without Textual. Changing the plan now goes through the commands,
but `tui.py` still decides things: `action_write` works out the warnings
(which `git recommit write` doesn't give), and `_build` runs the validation
flow, which `cli.write` does its own way. Writing could be a command too, with
the TUI and the command line differing only in how they ask what to do when
validation fails. And `session.py` does five jobs (resolving the range, the
working tree's commit, starting from the old commits, building, finishing):
starting from the old commits (~100 lines) could be a module of its own.

Some names are hard to follow: `head`, `tip` and `_tip_tree`; `pre`, `eff`,
`pure` and `prev_*` in `snapshot.file_states`; `index` for both a commit's
place and git's index in `Session.build`; and the module `diffs` beside
variables named `diff`.

## `a...b` means the same as `a..b`

In git, `a...b` is the symmetric difference; git recommit takes either as "the
commits between a and b, whichever way round". It's documented, but
surprising.

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
