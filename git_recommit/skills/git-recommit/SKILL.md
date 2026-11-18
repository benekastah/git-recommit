---
name: git-recommit
description: Rebuild or split commits with git recommit's non-interactive command line, when the user asks for it.
disable-model-invocation: true
---

# Rebuild commits with git recommit

Use git recommit when the user asks to rebuild a run of commits into
reviewable ones, or to split uncommitted changes into commits. Use its
non-interactive subcommands, never the TUI (`git recommit <range>`,
`git recommit tui`): it needs a person at the keyboard.

Its options change, so read them first: `git recommit -h`, then
`git recommit <subcommand> -h` for each subcommand you use.

1. Check the range and the worktree before starting. The range must end at
   HEAD. Tracked uncommitted changes are part of the diff, as if committed
   after HEAD; `-u` adds untracked files. `HEAD` as the range splits the
   uncommitted changes alone. The old commits are replaced, so make sure
   nobody else has built on them.
2. `git recommit start <range>` begins from the commits as they are, kept as a
   plan in the git directory until `write` or `abort`. `git recommit show --json` shows the plan: the commits by number (from 1), and the changes
   with the numbers that name them.
3. Shape the plan with `commit add|remove|move|reword|author`, `assign`,
   `unassign` and `edit`:

   ```sh
   git recommit commit add "Add the API" --after 2
   git recommit assign 3 src/api.py src/util.py:@2 src/main.py:+10-14
   git recommit unassign src/main.py:-3
   git recommit edit 3 src/api.py < stub.py    # the file in commit 3, by hand
   git recommit edit 2 scratch.txt --delete
   ```

   A target is `PATH` (the whole file), `PATH:@N` (its Nth hunk; `@0` is the
   change to the file itself, like its mode or a rename), `PATH:+N-M` (added
   lines, numbered as after the changes) or `PATH:-N-M` (removed lines,
   numbered as before). `PATH` is a git pathspec, relative to the current
   directory. `edit` reads the file's whole content from stdin; later commits
   carry the edit, and it can conflict with what they take, which `show`
   reports and the conflict's own commit resolves with another `edit`. Run
   `git recommit show --json` again after each change: the numbers move.
4. `git recommit write --validate '<test command>'` makes the new commits and
   moves the branch to them. It runs the command on each new commit first, and
   stops at the first failure without changing anything. Unassigned changes
   stay uncommitted in the working tree. `git recommit abort` drops the plan and
   changes nothing.
5. `write` prints how to get the old commits back (`git reset <old HEAD>`; they
   are in the reflog too). Make sure each resulting commit is independently
   correct, and run the checks that suit the change.
