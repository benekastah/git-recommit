# git-recommit's own rules

What's true of git-recommit alone. `AGENTS.md` has the rules it shares with
every project made from python-template; where the two disagree, this file
wins. Keep this file current when the shape of the code or a rule here
changes. `docs/issues.md` lists known issues, most important first, and
`docs/roadmap.md` planned upgrades and the options for them.

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
  submodule as a whole, a mode change, a rename, adding or deleting an empty
  file). A renamed file is one `FileDiff`, at its old path until a commit
  takes the rename.
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
- `git_recommit/skills/<name>/SKILL.md` are the agent skills the package ships
  (in the package, so an installed copy has them). A skill states only what the
  command line and README document, none of one user's workflow;
  `tests/test_skills.py` checks that its name is its directory's and that every
  `git recommit ...` command it shows parses with `cli.py`'s parsers.
  `git_recommit/skills.py` links them into each agent's skills directory
  (`install-skills`); `AGENTS` there is the list of agents it knows, and
  supporting another is one entry.
  `dev.py install` links them too, to the clone's, by running the clone's
  `install-skills` (`after_install()` in `dev_project.py`), so a developer's
  agents use the code being changed and there's one implementation of the
  logic (a refused link stops the install; the junction on Windows comes
  with it, which `install_links()`' symlinks wouldn't).

## Decisions to keep

These were the user's choices for v0; ask before changing them.

- Follow git's conventions wherever possible; it's a git extension. E.g.
  paths the user types are pathspecs (relative to the current directory,
  with wildcards and magic, matched by git itself), `-C` adds up as git's
  does, messages are cleaned by `commit.cleanup` and `core.commentChar`, and
  commits are signed per `commit.gpgSign`. And don't change git's behavior
  for the user: e.g. no environment variables that their hooks would
  inherit. (py-git-client's GIT_TEXTDOMAINDIR, which keeps git's messages in
  English, is the one exception: hooks and filters keep the user's locale.)

- The TUI uses Textual. Use the latest stable version of any library added,
  and add libraries sparingly: dev dependencies are fine, runtime ones need a
  good reason.
- Layout: a sidebar on the left lists the new commits being built; the main
  panel on the right is the diff, where you pick lines or changes. The key
  bindings are shown along the top.
- Keys are vim-like: `j`/`k` (or arrows) move, `h`/`l` scroll sideways, `V`
  selects lines. `f` assigns the whole file to the active commit.
- An edit made by hand to a file in one new commit is carried forward into
  later commits, merged with the changes they take (`git merge-file`; a binary
  file or symlink can't be merged, nor can content with a NUL in its first
  8000 bytes, even where attributes have it diffed as text, so a change on
  both sides conflicts), and can conflict with them; conflicts must be
  resolved before writing. An edit is kept as (basis, content), so it's merged
  again when what it was based on changes.
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
  authorship beyond that are in `docs/roadmap.md`.
- `ctrl+z` suspends; on resuming (and on `R`), the diff is reloaded from the
  working tree and HEAD, and assignments carry over by matching changes on
  `diff.change_keys` (path, kind, text, which occurrence), since ids change.

## Tests

- Tests run git-recommit against real repositories (`tests/helpers.py`,
  `tests/conftest.py`), under py-git-client's pytest plugin
  (`git_client.testing`): each test's git has a config of its own, and runs
  from a directory outside any repository. Only tests may import it (it
  imports pytest). `Repo.recommit` runs it in the test's process, so a
  monkeypatch on git_recommit reaches it; with `env`, it runs it in its own.
  `tests/test_tui.py` drives the app headless with Textual's pilot and a
  fake `Terminal`. What the headless driver can't do (e.g. ctrl+z really
  stopping the process) is tested in `tests/test_terminal.py`, which runs
  git recommit as a job in a pseudo-terminal, under `tests/job_control.py`
  (Unix only).

## Where it differs from the template

- The package is `git_recommit/`, not `src/git_recommit/`, and Ruff's lines
  are 120 columns. `dev.py` is left out of formatting (it's the template's, at
  its 100), and out of the Ruff rules this project adds (C901, PERF401, ANN).
- basedpyright checks as if on Linux; `python3 dev.py types-windows`
  (`dev_project.py`) checks again as if on Windows, for the code only
  Windows runs. CI runs it in `.github/workflows/project.yml`.
- CI's time limits in `.github/workflows/ci.yml` (the template's) are set to
  this project's times, each edit marked "git-recommit:".
- Two tests fake a program with a shell script, and are skipped on Windows:
  gpg, to check that commits are signed (`tests/test_session.py`), and a git
  wrapper, to check that every git process is the one git-recommit names
  (`tests/test_git.py`). That's deliberate: each fakes what git itself runs,
  in the way git runs it. They have no Windows counterpart yet (see
  `docs/issues.md`).

## Linux, macOS and Windows

- `os.path` is fine for paths, as well as `pathlib`. Run git through
  `GitClient`, with arguments as a list, never through a shell. The editor
  and the validate command are the user's shell commands, so those do run
  through one. `GitClient` (`git_recommit/git.py`) is py-git-client's `Git`
  (`git_client`), which starts git by its full path, the quickest way each
  system allows; it adds the reads git recommit makes, and its `run`,
  `run_bytes` and those reads raise `RecommitError`s rather than
  `GitError`s (what it has of `Git`'s as it is, e.g. `var` and `resolve`,
  raises `GitError`s; `succeeds` and `passthrough` say whether git
  succeeded, though a timeout still raises `GitTimeoutError`). git's
  messages that it captures are in English, as py-git-client has them;
  passthrough's are in the user's language. The tests' own git calls
  (`Repo.git`) use `Git` too, with the real git (`helpers.GIT`).
- Read from git with as few processes as you can: names and objects through
  `GitClient`'s one `cat-file` process (`commit_sha`, `head`, `tree`,
  `read_object`), and one git command for many things (e.g. one
  `diff-tree --stdin` for every commit) over one each.
- Read plumbing's output (`rev-list`, `diff-tree`, `ls-tree`, `cat-file`),
  not porcelain's (e.g. `git log`), which the user's config changes (e.g.
  `diff.relative`). Even plumbing leaves out submodules that
  `submodule.<name>.ignore` ignores, so diffs say `--ignore-submodules=none`.
  Where there's no plumbing for it, use the format git keeps for scripts
  (`status --porcelain=v2`, `blame --porcelain`).
- diff.py keeps every line's own ending, and splits only at `\n`, as git
  does.
