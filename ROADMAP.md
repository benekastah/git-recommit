# Roadmap

Planned upgrades, and the options for them. Known problems are in ISSUES.md.

## Preserving authorship

Each new commit that starts from an old one keeps that commit's author and
author date (the committer is always whoever runs git recommit, as with a
rebase). A commit created with `c` gets the current user, even if every change
it takes is someone else's. Ways to do better, which can be combined:

1. **Inherit from the changes' commits.** Give a commit the author of the old
   commit most of its changes came from (by changed lines, which git blame
   already tells us), and its latest author date. It needs no input, and is
   right when one person's work is being reorganized; with mixed authors, the
   majority wins, which may not be who should be credited.
2. **Co-authored-by trailers.** Add a `Co-authored-by: Name <email>` trailer
   for each other author whose changes a commit takes, as GitHub and GitLab
   show. It credits everyone without choosing one, and pairs well with 1.
   The message editor (`r`) would show them, so they can be removed.
3. **Choose the author.** A key to set the active commit's author: from a list
   of the range's authors, or typed. Explicit, and the fallback when 1 picks
   wrongly. The sidebar would show each commit's author when it isn't the
   current user, so a wrong one is noticed before writing.
4. **Follow the commit a new one replaces.** When a commit is split (its
   changes moved to a new commit after it), the new one inherits its author.
   Covers the common case of splitting a commit, but depends on how the new
   commit was made, which is harder to explain than 1.
5. **Warn when writing.** List the commits whose changes came from someone else
   but which will be authored by the current user, in the confirmation, so
   nothing is reattributed by accident. Cheap, and worth doing whatever else
   is chosen.

A reasonable path: 5 first, then 1 with 2 for the other authors, then 3.

## A command line for everything the TUI does

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

## Giving a commit a file the range doesn't change

See the first item in ISSUES.md: a key to edit any path in the active commit,
and rows for files with no net change.
