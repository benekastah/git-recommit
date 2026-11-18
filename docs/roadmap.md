# Roadmap

What's left to build in git-recommit. Only what's still to do lives here: when something
is done, remove it in the same change. What's wrong with what's built is in
[issues.md](issues.md).

Each item gives the options for it.

## Preserving authorship

Done so far: a commit that starts from an old one keeps its author and date;
writing warns about any commit authored by someone other than who made some
of its changes (git recommit knows who made each, from git blame); and `A`, or
`git recommit commit author`, sets a commit's author, as `git commit --author`
does. Ways to do more, which ask nothing of the user but change commits
without being asked, so would need to be options:

1. **Inherit from the changes' commits.** Give a commit the user creates the
   author of the old commit most of its changes came from (by changed lines),
   and its latest author date. Right when one person's work is reorganized;
   with mixed authors, the majority wins, which may not be who should be
   credited.
2. **Co-authored-by trailers.** Add a `Co-authored-by: Name <email>` trailer
   for each other author whose changes a commit takes, as GitHub and GitLab
   show, crediting everyone without choosing one. Pairs well with 1.
3. **Follow the commit a new one is split from.** When a commit's changes are
   moved to a new commit after it, the new one inherits its author. Covers
   splitting a commit, but depends on how the new commit was made, which is
   harder to explain than 1.

## A command line for everything the TUI does

Done, but for undo and the log: see README.md's "From the command line", and
`commands.py`, which both the TUI and the command line run. What's left, and
how it was designed, follows.

So that a script or an agent can rebuild commits too: each thing the TUI does
as a subcommand, working on a plan kept in a file between commands, and a last
command that writes it.

```sh
git recommit start HEAD~6          # write the plan: the old commits, as the TUI starts
git recommit show [--json]         # the commits, and the changes with their ids and assignments
git recommit commit add "Add the API" --after 2
git recommit assign 2 src/api.py   # a file, a hunk (src/api.py:@3) or lines (src/api.py:+10-14)
git recommit edit 1 src/api.py < stub.py
git recommit write [--validate 'pytest']
git recommit abort                 # remove the plan
```

One set of commands, for both. Each command is a Python function (or a small
dataclass with a `run`) on the session: `assign(session, target, commit)`,
`add_commit(session, message, after)`, `edit(session, commit, path,
content)`, `write(session, validate)`, and so on. The command line parses its
arguments into a call, loading the plan before and saving it after; the TUI's
keys make the same calls in-process, on the session it holds. So everything
the TUI can do, the command line can, and they can't drift apart: a TUI
action that isn't a command is a missing command. (`board.py` is most of the
way there: its methods would become the commands, taking targets instead of
reading the cursor.)

Commands as data (a name and arguments) also give undo and a log for free:
keep the commands run since `start`, and undo by replaying all but the last
(or by keeping the plan before each). An agent's transcript is then a list of
commands that can be read, replayed or turned into a test.

What to decide:

- **Where the plan lives.** In the git directory (`git rev-parse --git-path
  recommit/plan.json`), not a temp file: it's per worktree, the next command
  finds it without being told, it survives a reboot, and `git clean` doesn't
  touch it. One plan at a time, like a rebase in progress; `start` refuses if
  there is one, and `abort` or `write` removes it.
- **JSON.** The plan file is what `Plan` and the session hold: base, HEAD and
  the tip's tree (so a command can tell the repository changed since, and
  reload as the TUI does on resuming), the commits (message, author, edits),
  and the assignments.
- **Naming changes stably.** Change ids are positions in the diff, so they
  shift when the diff changes. The file should store changes by
  `diff.change_keys` (path, kind, text, which occurrence), as reloading does,
  and the commands take paths, hunks and line ranges, which read well in a
  shell. `show --json` gives an agent everything at once.
- **Undo.** Keep each version of the plan (or each command, as above), so
  `git recommit undo` steps back. Cheap, and makes experimenting safe for an
  agent, which is the point. The TUI gets it too, as `u`, since it runs the
  same commands.
- **The TUI on a plan.** `git recommit tui` could open the plan in the TUI,
  and leave it as a plan on quitting, so a person can finish what an agent
  started, or the reverse.

## Rebuilding a range with merges

Today a range with merge commits in it (or after it, up to HEAD) is refused:
the new commits are one line of history, so there's nowhere for a merge's
other parent to go. `git rebase` (without `--rebase-merges`) has the same
problem, and drops the merges, putting the other commits in one line.
git recommit could do the same.

The common case is merging the branch a feature started from back into it,
then rebuilding the feature: `git recommit main..HEAD`. The range's base is
then main's tip, and main's commits aren't in the range, so the diff is just
the feature's changes, and the new commits go in one line on main: a rebase
and a recombine at once. Dropping a merge whose other side has commits in
the range puts them in one line too, as rebase does.

What to decide:

- **Starting from the old commits.** Commits before the merge were made on
  the old base, so reproducing their files exactly (`oldcommits._reproduce`)
  would undo the merged changes. Those commits could take their changes by
  git blame alone, without edits that reproduce them.
- **The merge's own changes.** Conflict resolutions (and any other change the
  merge itself makes) blame to the merge; it could be an old commit like the
  others, with its message, or left out if it makes no changes of its own.
- **Saying so.** Dropping commits isn't something to do silently; the TUI
  and `start` should say which merges were dropped.

## Editing a hunk

Not planned for now: `e` edits the whole file, which covers everything, if
clumsily for a long file. Opening only the hunk's part of the file was
considered and set aside, since finding that part in the file as a commit has
it (with the edits carried in) is fuzzy, and the editor wouldn't show the
code around it.

What would be simpler: with the cursor on a hunk (or lines selected), `e`
first does what `a` does (assigns the hunk to the active commit), then opens
the whole file as the commit now has it. Cancelling (quitting without saving,
which leaves the content as it was, or making the editor fail, e.g. vim's
`:cq`) undoes both. On the file's own row, `e` would stay as it
is, so pressing it doesn't assign anything by surprise. The command line
could take it as `git recommit edit N path --with path:@3`, printing the file
to start from.

And to make a long file easy, the editor could zoom in on the hunk. Which
editor it is comes from `git var GIT_EDITOR`, a shell command: split it as
the shell does, and look at the first word's name. Only editors it knows get
extra arguments; any other opens as now. (Each editor's flags should be
checked against its documentation before relying on them.)

- **vim, nvim** (and `gvim -f`, `view`): fold away the code before and after
  the hunk, with `-c 'setlocal foldmethod=manual' -c 'normal! zE'`
  `-c '1,{a-1}fold' -c '{b+1},$fold' -c '{a}' -c 'normal! zz'`, which run
  after the file (and its ftplugin) loads; leaving out a fold where the hunk
  is at the start or end of the file.
- **emacs**: narrow to the hunk (`C-x n w` widens again), with
  `--eval '(progn (goto-char (point-min)) (forward-line {a-1}) ...)'` after
  the file. emacsclient: only `+{a}`.
- **vi** (nvi, busybox), **nano**, **micro**, **kakoune**: `+{a}`, the cursor
  on the hunk, as they can't fold from the command line.
- **helix**, **Sublime Text**, **Zed**: `file:{a}`; **VS Code**:
  `--goto file:{a}`. Cursor only too.

The hunk's lines in the file as the commit has it can be approximate (by a
line diff from what the changes alone make it), since zooming in is only a
view: the rest of the file is still there.

## Sharing the agent skills' code

`git_recommit/skills.py` (the agents `install-skills` links into, and how) and
`tests/test_skills.py` are the same as git-stack's, copied. Options:

1. **Keep a copy in each project.** Costs nothing now; they drift, and each
   fix is made once per project.
2. **python-template.** The test, and the rule that a skill states only what
   the CLI documents, are generic. The code is runtime code that ships in the
   package, which `copier update` handles poorly (the template's `cli.py` is
   left alone for that reason).
3. **A small shared library**, like py-git-client, holding the registry and
   the linking; the template holds the test. One more dependency to keep.

Also, Codex takes "only when asked" from `agents/openai.yaml`
(`allow_implicit_invocation: false`), not from the SKILL.md's
`disable-model-invocation`, so the skill may be used on its own there.
