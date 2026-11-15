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
git recommit main..HEAD                   # or a range (either way round, .. or ...)
git recommit HEAD                         # split up only your uncommitted changes
git recommit --edit HEAD~3                # change one commit, then replay the later ones on it
```

The range must end at `HEAD`, and can't have merge commits in it. Your
uncommitted changes to tracked files (staged or not) are included, after the
last commit, so you can assign them to new commits too; untracked files
aren't.

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
| `x`/`0` | unassign |
| `c` | create a commit, after the active one |
| `tab`/`shift+tab` | make the next/previous commit active (or click it in the sidebar) |
| `J`/`K` | move the active commit later/earlier |
| `r` | reword the active commit's message, in your editor |
| `D` | delete the active commit; its changes become unassigned |
| `C` | clear every commit, to start from scratch |
| `O` | go back to the commits being rebuilt, as they were at the start |
| `e` | edit the file as it is in the active commit, in your editor |
| `E` | drop the active commit's edit of the file |
| `ctrl+z` | suspend, to change files; when you bring it back (`fg`), it reloads the changes |
| `R` | reload the changes, e.g. after changing files in another terminal |
| `w` | write: rebuild the commits |
| `q` | quit without changing anything |
| `?` | every key |

Each new commit has every change assigned to it or to an earlier commit.

**Editing a file in a commit** (`e`) is for making a commit work on its own,
e.g. stubbing out a function that a later commit fills in. The edit is carried
into the later commits, merged with the changes they take, as a rebase would.
Where a later commit's changes overlap the edit, that commit conflicts: it's
marked in the sidebar, and you resolve it by editing the file in that commit,
which opens with conflict markers. You can't write until every conflict is
resolved.

`e` opens the file as it is in the active commit; if the commit doesn't have
the file yet, it opens the file's end state, for you to cut down. To cancel,
quit without saving (or make the editor fail, e.g. `:cq` in vim): saving what
was opened changes nothing.

**Changing your mind** about the code itself: press `ctrl+z`, change files (or
even commit), then `fg`. git-recommit reloads the diff, and changes you'd
assigned keep their commits; new ones are unassigned.

**Writing** (`w`) asks first, and warns about anything that isn't assigned to
a commit. Changes you leave unassigned aren't lost: they're left as
uncommitted changes in your working tree, which is never touched (though
nothing is left staged). With
`--validate`, the command runs in a scratch worktree with each new commit
checked out in turn; if it fails, you choose to go back and **e**dit the
commits, **a**bort, or **p**roceed anyway.

The old commits are replaced, and the command prints how to get them back
(`git reset <old HEAD>`, which leaves your working tree as it is); they're
also in the reflog.

### --edit

`git recommit --edit <commit>` runs `git rebase -i` and stops at that commit.
Change it and `git commit --amend`, then `git rebase --continue` replays the
later commits, stopping for you to resolve any conflicts.

## Install

Needs Python 3.11 or later and git 2.43 or later.

```sh
pipx install git+https://github.com/benekastah/git-recommit
```

git runs `git-recommit` on your `PATH` as `git recommit`.

## Development

Clone the repository and run `./dev-install.sh`. It installs git-recommit in
editable mode into `~/.local/share/git-recommit/venv`, links `git-recommit`
into `~/.local/bin`, sets up the clone's `.venv` with the dev dependencies,
and makes the clone use the hooks in `git_hooks/` (the pre-commit hook formats
staged Python files, and the pre-push hook runs the checks).

Run every check (shellcheck, Ruff's formatter and linter, mypy in strict mode,
then the tests) with `./check.sh`; arguments are passed to pytest.
