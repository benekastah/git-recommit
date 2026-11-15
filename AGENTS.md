# Working on git-recommit

Notes for agents (and people) changing this repo. The README is the user
documentation; its Development section says how to set up a clone and run the
checks. `ISSUES.md` lists known issues, most important first, and `ROADMAP.md`
planned upgrades and the options for them; keep them up to date.

## What it does

`git recommit <range> [--validate CMD]` rebuilds the commits in a range (which
must end at HEAD) into new ones. A TUI shows the diff from the range's base to
its tip; the user creates new commits in a sidebar and assigns changes to
them: a line, a range of lines, a hunk or a whole file. Writing replaces the
old commits with the new ones, after running the validate command (if given)
on each, in a scratch worktree. `git recommit --edit <commit>` instead wraps
`git rebase -i`, stopping at that commit. Uncommitted changes to tracked files
are part of the diff, as if committed after HEAD (`session.worktree_commit`).

## Shape of the code

- `git_recommit/diff.py` loads the diff (with `git diff-tree`) and splits it
  into changes, each with an id: every added or removed line of a text file,
  plus one "meta" change for what can't be split (a binary file, symlink or
  submodule as a whole, a mode change, adding or deleting an empty file).
  `apply` makes a file's content with only some changes applied.
- `git_recommit/plan.py` is the new commits and which change ids each takes.
- `git_recommit/snapshot.py` says what each file is in each new commit:
  everything assigned to it or an earlier one, plus the user's edits.
- `git_recommit/session.py` ties those to a repository: the range's diff,
  the plan, building the new commits (with plumbing, in a temporary index,
  never touching the working tree), and moving HEAD's branch to them.
  `git_recommit/revisions.py` reads the range, and makes the uncommitted
  changes a commit; `git_recommit/oldcommits.py` starts the plan from the old
  commits (by git blame, with edits where that isn't enough).
- `git_recommit/writing.py` is writing, as the TUI and `git recommit write`
  share it: what stops it, the warnings, building and validating.
- `git_recommit/commands.py` is every change to the plan, as a command: a
  frozen dataclass with `run(session)`, naming commits by number and changes
  by `Target`s (`path`, `path:@2`, `path:+10-14`). The command line's
  subcommands and the TUI's keys both run them, so anything one can do, the
  other can; a change to the plan that isn't a command is a missing command.
- `git_recommit/planfile.py` keeps a recommit in progress between subcommands,
  as JSON in the git directory, with changes by `diff.change_keys`;
  `git_recommit/show.py` prints a plan, as text or JSON. `git_recommit/cli.py`
  parses the subcommands into commands, and runs the TUI for `git recommit
  <range>` (or `tui`).
- `git_recommit/board.py` is the TUI's state apart from drawing (rows, cursor,
  selection, active commit), and turns the cursor and selection into commands
  (`Board.run`); `git_recommit/tui.py` draws it with Textual and maps the
  keys. Keep logic in `board.py` or the commands, where it's tested without
  Textual.
  What the TUI does with the terminal itself (the editor, the validate command)
  goes through `tui.Terminal`, which tests replace.
- The sidebar is built from sections, one method each (`_new_commits_section`,
  ...), which `BoardScreen._sidebar` calls in order; reorder the calls to
  reorder the sections. Each line records what it's about (`Target`), which is
  how clicks find it, so sidebar lines must never wrap.
- `git_recommit/validate.py` runs the validate command; `edit_commit.py` and
  `sequence_editor.py` are `--edit`.

## Decisions to keep

These were the user's choices for v0; ask before changing them.

- Follow git's conventions wherever possible; it's a git extension. E.g.
  paths the user types are pathspecs (relative to the current directory,
  with wildcards and magic, matched by git itself), `-C` adds up as git's
  does, messages are cleaned by `commit.cleanup` and `core.commentChar`, and
  commits are signed per `commit.gpgSign`. And don't change git's behavior
  for the user: e.g. no environment variables that their hooks would
  inherit.

- The TUI uses Textual. Use the latest stable version of any library added,
  and add libraries sparingly: dev dependencies are fine, runtime ones need a
  good reason.
- Layout: a sidebar on the left lists the new commits being built; the main
  panel on the right is the diff, where you pick lines or changes. The key
  bindings are shown along the top.
- Keys are vim-like: `j`/`k` (or arrows) move, `h`/`l` scroll sideways, `V`
  selects lines. `f` assigns the whole file to the active commit.
- An edit made by hand to a file in one new commit is carried forward into
  later commits, merged with the changes they take (`git merge-file`), and can
  conflict with them; conflicts must be resolved before writing. An edit is
  kept as (basis, content), so it's merged again when what it was based on
  changes.
- Changes left unassigned are allowed, with a warning; they end up as
  uncommitted changes in the working tree. recommit finishes by moving HEAD's
  branch and resetting the index, never touching the working tree.
- Uncommitted changes can be assigned too: the diff runs to a commit of the
  working tree (tracked files, as `git stash` would), so they're changes like
  any other, and those left unassigned simply stay uncommitted.
- `--edit <commit>` wraps `git rebase -i`.
- The plan starts as the old commits (`Session.restore_old_commits`): each
  keeps its message and author, takes the changes git blame says it made, and
  gets edits where that alone wouldn't reproduce it. `C` clears the plan, `O`
  restores it. Commits the user creates are authored by them. Ideas for
  authorship beyond that are in `ROADMAP.md`.
- `ctrl+z` suspends; on resuming (and on `R`), the diff is reloaded from the
  working tree and HEAD, and assignments carry over by matching changes on
  `diff.change_keys` (path, kind, text, which occurrence), since ids change.

## Tests and checks

- Work test-first: write the tests for a change (or a bug) first, run them and
  see them fail for the reason you expect, then make the change and see them
  pass. If the tests need the code reshaped first (e.g. injecting a dependency
  they replace), do only that before seeing them fail: refactors that let the
  tests run, not ones that fix the bug, so the failure still shows the old
  behavior, or a good approximation of it. The failing tests can be committed
  on their own, before the commit that fixes them.
- Every change to behavior comes with tests, in the same commit. Tests say
  what's correct, not what the code happens to do.
- Tests run git-recommit against real repositories (`tests/helpers.py`,
  `tests/conftest.py`). `tests/test_tui.py` drives the app headless with
  Textual's pilot and a fake `Terminal`. What the headless driver can't do
  (e.g. ctrl+z really stopping the process) is tested in
  `tests/test_terminal.py`, which runs git recommit as a job in a
  pseudo-terminal, under `tests/job_control.py` (Unix only).
- Run `./check.sh` (shellcheck, Ruff format and lint, mypy strict, tests)
  before committing. The pre-push hook runs it too.
- Update the README with behavior changes.

## Linux, macOS and Windows

git-recommit should run on all three (CI runs the tests on each).

- Use `os.path` or `pathlib` for paths. Run git through `GitClient`, with
  arguments as a list, never through a shell. (The editor and the validate
  command are the user's shell commands, so those do run through one.)
- Don't assume `\n` line endings in files: diff.py keeps every line's own
  ending, and splits only at `\n`, as git does.
- Shell scripts (`check.sh`, the hooks) are for development only.
