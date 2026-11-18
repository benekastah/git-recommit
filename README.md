# git-recommit

Rebuild a run of commits into new ones that make sense to review, choosing in a
TUI which changes go in each.

You made a feature in a dozen commits, and they tell the story of how you got
there: the first version is very different from the last. `git recommit`
shows you where you ended up, as one diff, and you build new commits from it,
a line, a hunk or a file at a time. If you ask it to, it runs your tests on
each new commit before replacing the old ones.

## Usage

```sh
git recommit HEAD~6                       # rebuild the last 6 commits
git recommit HEAD~6 --validate 'pytest'   # ...and run pytest on each new commit
git recommit main..HEAD                   # or a range, as git reads it (.. or ...)
git recommit HEAD                         # split up only your uncommitted changes
git recommit -u HEAD~2                    # with untracked files too
git recommit --root                       # every commit, as git rebase --root
git recommit --edit HEAD~3                # change one commit, then replay the later ones on it
```

The range is read as git reads ranges: `a..b` is the commits in `b` but not
`a` (so `main..HEAD` is those since your branch left `main`), `a...b` those
on either side but not both, and `a` alone means `a..HEAD`. It can end below
`HEAD` (e.g. `HEAD~6..HEAD~2`): then the commits after it are replayed on the
new ones, as `git rebase` does, which needs the last new commit to end as the
range does (every change assigned). It can't have merge commits in it, or
after it. Your
uncommitted changes to tracked files (staged or not) are included, after the
last commit, so you can assign them to new commits too. Untracked files are
too with `-u` (`--include-untracked`), as with `git stash -u`, but for ignored
ones; without it, the sidebar says how many are left out. Untracked files you
don't assign are left untracked.

### The TUI

The sidebar on the left lists the new commits you're building; the one marked
▶ is the *active* commit; click one to make it active. Below them is the
range being replaced (check it's the one you meant), then the files, each
with how many of its changes are assigned; click one to go to it.

The new commits start out as the commits being rebuilt, with their messages
and authors, each taking the changes it made. Where an old commit's changes
overlap a later one's (e.g. it added a line a later commit rewrote), it gets
an edit (✎) so that it's as it was. Writing them unchanged makes the same
commits again. Change what you like: drop an edit (`E`), move changes, add or
delete commits. `C` clears them all, to start from scratch, and `O` brings
them back as they were.
The sidebar scrolls when it's too long. The main panel is the diff from the start of the
range to the end, with each line's numbers before and after, and a rule
across the panel where each hunk starts. Each change is marked, at the far
left, with the number of the commit that takes it (`·` for none yet). The keys
are along the top.

| Key | Does |
| --- | --- |
| `j`/`k`, `↓`/`↑` | move down/up (`h`/`l`, `←`/`→` scroll sideways), or click a row (shift+click selects to it) |
| `]`/`[`, `}`/`{` | next/previous file, hunk |
| `.` | next unassigned change |
| `V` (or `v`) | start or stop selecting lines; `Esc` cancels |
| `space`/`enter` | assign the line (or the selected lines, or on a header, its hunk or file) to the active commit |
| `a` / `f` | assign the whole hunk / file the cursor is in to the active commit |
| `1`-`9` | assign to that commit (and make it active) |
| `#` | assign to the commit whose number you type (for commit 10 and on) |
| `x`/`0` | unassign |
| `c` | create a commit, after the active one |
| `tab`/`shift+tab` | make the next/previous commit active (or click it in the sidebar) |
| `J`/`K` | move the active commit later/earlier |
| `r` | reword the active commit's message, in your editor |
| `A` | set the active commit's author (`Name <email>`; empty for you) |
| `D` | delete the active commit; its changes become unassigned |
| `C` | clear every commit, to start from scratch |
| `O` | go back to the commits being rebuilt, as they were at the start |
| `e` | edit the file as it is in the active commit, in your editor (a binary file: replace it with another file) |
| `E` | drop the active commit's edit of the file |
| `+` | edit a file the range doesn't change (or that isn't there yet) in the active commit, as `e` |
| `ctrl+z` | suspend, to change files; when you bring it back (`fg`), it reloads the changes |
| `R` | reload the changes, e.g. after changing files in another terminal |
| `w` | write: rebuild the commits |
| `q` | quit without changing anything |
| `?` | every key |

Each new commit has every change assigned to it or to an earlier commit.

**A renamed file** is one file, shown as `R old -> new`, as `git status`
shows it, found as `git diff` finds renames (unless `diff.renames` is off).
The rename is a change of its own, on a row under the file's name, apart
from the changes to its lines (but together with a change of its mode, if it
has one): until a commit takes the rename, the file is at its old path, with
whatever lines it takes. `f` assigns all of it.

**Editing a file in a commit** (`e`) is for making a commit work on its own,
e.g. stubbing out a function that a later commit fills in. The edit is carried
into the later commits, merged with the changes they take, as a rebase would.
Where a later commit's changes overlap the edit, that commit conflicts: it's
marked in the sidebar, and you resolve it by editing the file in that commit,
which opens with conflict markers (but for a binary file or symlink, or a
file git takes for binary, below).
You can't write until every conflict is resolved.

`e` opens the file as it is in the active commit; if the commit doesn't have
the file yet, it opens the file's end state, for you to cut down. To cancel,
quit without saving (or make the editor fail, e.g. `:cq` in vim): saving what
was opened changes nothing.

`+` does the same for a file the range doesn't change (or that isn't there at
all), e.g. to stub one out for a commit and put it back in a later one; it's
shown as `= path`.

A binary file doesn't open in an editor: `e` asks for a file to replace it
with in the commit (relative to the current directory). A symlink opens as
where it points. Since neither can be merged, a later commit that changes the
file too conflicts with the edit, until you replace (or edit) it there as
well. There, it opens as your edit made it (there's nothing to mark), and
saving it unchanged (or giving the same file) keeps that, resolving the
conflict; so to cancel, make the editor fail (`:cq`). Submodules can't be
edited.

A file with a NUL in its first 8000 bytes is one git takes for binary. If its
attributes have it diffed as text (e.g. `diff`), it opens in the editor, but
git won't merge it, so it conflicts as a binary file does, and opens the same
way.

Where the edit carried in deleted such a file (or a binary file or symlink),
the commit that conflicts doesn't have it, so `e` opens its end state (or, for
a binary file, asks for a file). Saving that unchanged (or giving that same
file) keeps the deletion, resolving the conflict, as does
`git recommit edit N path --delete`. To have the file there as it ends up
instead, edit it to anything, then back.

**Changing your mind** about the code itself: press `ctrl+z`, change files (or
even commit), then `fg`. git-recommit reloads the diff, and changes you'd
assigned keep their commits; new ones are unassigned.

**Writing** (`w`) asks first, and warns about anything that isn't assigned to
a commit. Changes you leave unassigned aren't lost: they're left as
uncommitted changes in your working tree, which is never touched (though
nothing is left staged, but for new files, which are left `git add
--intent-to-add`ed, so they're still new files rather than untracked ones). With
`--validate`, the command runs in a scratch worktree with each new commit
checked out in turn (with your `post-checkout` hook, as any checkout
runs it, e.g. for Git LFS); if it fails, you choose to go back and **e**dit the
commits, **a**bort, or **p**roceed anyway.

A commit that starts from an old one keeps its author; one you create is
yours, as with `git commit`. Writing warns about any commit whose author isn't
who made some of its changes, and `A` (or `git recommit commit author`) sets a
commit's author, as `git commit --author` does.

New commits are signed if `commit.gpgSign` says to, and the `commit-msg` hook
runs on the messages of commits you create or reword (not on old commits'
messages you kept), as with `git rebase`; no other hooks run.

The old commits are replaced, and the command prints how to get them back
(`git reset <old HEAD>`, which leaves your working tree as it is); they're
also in the reflog.

### From the command line

Everything the TUI does can be done a step at a time from the command line,
e.g. in a script, or by an agent. `start` begins a recommit, kept as a plan in
the git directory (like a rebase in progress) until `write` makes the new
commits, or `abort` drops it. Commits are named by number, from 1, and changes
by *targets*: `PATH` (the whole file), `PATH:@N` (its Nth hunk; `@0` is the
change to the file itself, like its mode or renaming it), `PATH:+N-M` (added
lines, numbered as after the changes) or `PATH:-N-M` (removed lines, numbered
as before). `show` shows the numbers. `PATH` is a git pathspec, as in `git
add`: relative to the current directory, a directory for the files in it,
wildcards like `'src/*.py'`, and magic like `:/` (from the top) or
`:(literal)`; a hunk or lines need it to match one file. A renamed file goes
by either of its paths.

```sh
git recommit start HEAD~6               # start from the 6 commits as they are
git recommit show                       # the commits, and the diff marked with them
git recommit show --json                # all of it, for scripts
git recommit commit add "Add the API" --after 2
git recommit assign 3 src/api.py src/util.py:@2 src/main.py:+10-14
git recommit unassign src/main.py:-3
git recommit edit 3 src/api.py < stub.py    # the file in commit 3, by hand
git recommit edit 3 logo.png < old-logo.png # (a binary file too)
git recommit edit 2 scratch.txt --delete    # (any file, changed by the range or not)
git recommit commit move 3 1                # or remove, or reword
git recommit commit author 3 "Ana <ana@example.com>"
git recommit write --validate 'pytest'
git recommit tui                        # or carry on in the TUI (quitting keeps the plan)
git recommit abort                      # or drop it
```

The other subcommands are `drop-edit`, `clear`, `restore` and `install-skills`
(see below); `git recommit
<subcommand> -h` says more. With `--validate`, `write` stops at the first
commit the command fails on, changing nothing.

### --edit

`git recommit --edit <commit>` runs `git rebase -i` and stops at that commit.
Change it and `git commit --amend`, then `git rebase --continue` replays the
later commits, stopping for you to resolve any conflicts.

### Agent skills

A skill for coding agents, `git_recommit/skills/git-recommit/SKILL.md`, tells
an agent how to rebuild commits with the command line above, never the TUI. It
asks not to be used unless you ask, which Claude Code honours and other agents
may not. It ships in the package, and

```sh
git recommit install-skills
```

links it into the skills directory of each agent it finds you use (`--claude`
and `--codex` name one, even if it isn't found; `--json` says what it did): Claude's
is `~/.claude/skills`, or `$CLAUDE_CONFIG_DIR/skills`; Codex's is
`~/.agents/skills`. The links point into the installed package, so upgrading
git-recommit updates them. A link already right is left alone. A link to
another directory (another clone's, or a skill of your own) is left too, with
nothing linked anywhere, and it stops saying so; `--replace` takes it over. A
link whose target is gone is replaced. A real directory or file is never
touched: move it away. On Windows, where a symlink needs a privilege, it makes
a junction.

## Install

Needs Python 3.15 or later and git 2.43 or later.

```sh
pipx install git+https://github.com/benekastah/git-recommit
```

git runs `git-recommit` on your `PATH` as `git recommit`. It runs the first
`git` on your `PATH` in turn (as `git recommit`, that's the git that ran it:
git puts its exec-path first), or the one `GIT_RECOMMIT_GIT` names, if set.
Only directories on `PATH` given as absolute paths count. On Windows, it runs
`git.exe`, never a batch file (`.bat`, `.cmd`, e.g. an npm shim), which would
read `&`, `|` and `%` in git's arguments; `GIT_RECOMMIT_GIT` can't name one.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone
the repository, then run `python3 dev.py install` (on Windows,
`py dev.py install`). It sets up the clone's `.venv` with git-recommit
(editable) and the dev tools, from `uv.lock`, links `git-recommit` into
`~/.local/bin` and its man page into `~/.local/share/man/man1` (not on
Windows), links the clone's agent skill into the skills directory of each agent
you use (see [Agent skills](#agent-skills); on Windows, run `git recommit
install-skills` for that), and makes the clone use the hooks in `git_hooks/`: the pre-commit
hook formats and lint-fixes staged Python files and makes the man page again,
and the pre-push hook runs the static checks before every push. CI runs the
tests.

If `~/.local/bin` already has `git-recommit`, or an agent's skills directory
a `git-recommit` skill, from another clone (or installed another way),
`dev.py install` stops and says so, changing nothing:
`dev.py install --replace` takes it over (except, on Windows, another
install's `git-recommit.exe`, which you uninstall). A link to a clone that's
since been deleted it just replaces. It won't install a clone in a temporary
directory (the system's, `/tmp` or `/var/tmp`), which would leave the command
broken once it's deleted: there, run `python3 dev.py venv` and
`git config core.hooksPath git_hooks`, and use `.venv/bin/git-recommit` (on
Windows, `.venv\Scripts\git-recommit.exe`).

```sh
python3 dev.py check          # every check: static, then tests
python3 dev.py static         # Ruff's formatter and linter, basedpyright (strict), the man page
python3 dev.py tests          # the tests, in parallel (arguments go to pytest)
python3 dev.py coverage       # the tests under coverage.py, and the lines they don't run
python3 dev.py types-windows  # basedpyright as if on Windows (dev_project.py)
python3 dev.py man            # man/git-recommit.1, the man page, from this README
```

The man page, `man/git-recommit.1`, is made from this README by `dev.py man`,
with pandoc (from the `pypandoc_binary` dev dependency): its sections but
Install and Development, with the SYNOPSIS in `dev_project.py`. The pre-commit
hook makes it again when a commit changes the README, and adds it to the
commit (or stops a `git commit PATH`, which it can't add the page to: run
`dev.py man` and `git add` it, then commit). `dev.py static` (so the pre-push
hook, and CI) fails if it's out of date. Once `dev.py install` has linked it
(a `pipx install` doesn't install it), `man git-recommit` and
`git recommit --help` open it; if `dev.py install` warns that `man` doesn't
look in `~/.local/share/man`, add
`export MANPATH="$HOME/.local/share/man:"` to your shell's startup file.
Windows has no `man`: there, `git recommit --help` opens Git for Windows' HTML
help, which has no page for it, so use `git-recommit --help` or
`git recommit -h`.

The tests run git itself (git-core's, from `git --exec-path`), not a wrapper
on your `PATH`, and set `GIT_RECOMMIT_GIT` to it.

`dev.py` installs the dev dependencies at the versions locked in `uv.lock`,
and updates it when you change the dependencies in `pyproject.toml`; commit
both. Dependabot proposes new versions weekly.

CI (`.github/workflows/ci.yml`) runs the static checks once, on Linux, the
tests on Linux, macOS and Windows with the oldest and newest Python
supported, and a coverage report, along with this project's own jobs in
`.github/workflows/project.yml` (the type check as if on Windows). It installs
from `uv.lock` and fails if it's out of date.

### Updating from the template

The tooling comes from the [python-template](https://github.com/benekastah/python-template)
Copier template; take in its later changes with `uvx copier update --defaults`.
An update never changes this README, `PROJECT_AGENTS.md`, `docs/issues.md`,
`docs/roadmap.md` or `.github/workflows/project.yml`: they're this project's
own. `dev.py`, `AGENTS.md`, `CLAUDE.md`, `.github/workflows/ci.yml`,
`.github/workflows/cancel.yml` and `.github/actions/setup` are the
template's: this project's own commands go in `dev_project.py`, its rules for
agents in `PROJECT_AGENTS.md`, and its CI jobs in `project.yml`. Its edits to
`ci.yml`, the time limits, are each marked "git-recommit:".
