import argparse
import json
import sys
from collections.abc import Callable

from git_recommit import commands, planfile, show, validate
from git_recommit.commands import Command, Target
from git_recommit.edit_commit import edit_commit
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
from git_recommit.session import Session

SUBCOMMANDS = (
    "start",
    "show",
    "commit",
    "assign",
    "unassign",
    "edit",
    "drop-edit",
    "clear",
    "restore",
    "write",
    "abort",
    "tui",
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git recommit",
        description="Rebuild a run of commits into new ones, choosing in a TUI which changes each new commit takes.",
        epilog="RANGE is <base> (the commits after base, up to HEAD), <a>..<b> or <a>...<b>; e.g. HEAD~6. "
        f"To do it from the command line instead, a step at a time, see `git recommit start -h` and the other "
        f"subcommands: {', '.join(SUBCOMMANDS)}.",
    )
    p.add_argument("-C", dest="directory", default=".", help="run in this directory, as git -C does")
    p.add_argument("range", nargs="?", metavar="RANGE", help="the commits to rebuild")
    p.add_argument(
        "--validate", metavar="COMMAND", help="a shell command to run with each new commit checked out, e.g. 'pytest'"
    )
    p.add_argument(
        "--edit",
        metavar="COMMIT",
        help="instead, stop at COMMIT to change it, then replay the later commits on it (with git rebase)",
    )
    return p


def subcommand_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git recommit",
        description="Rebuild commits a step at a time: `start` a recommit, change its plan with the other "
        "subcommands, then `write` it. Commits are named by number, from 1; changes by targets: PATH (the "
        "whole file), PATH:@N (its Nth hunk; @0 is the change to the file itself, e.g. its mode), PATH:+N[-M] "
        "(added lines, numbered as after the changes) or PATH:-N[-M] (removed lines, numbered as before). "
        "`show` shows the numbers.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="start a recommit of RANGE, from its commits as they are")
    start.add_argument("range", metavar="RANGE")
    shown = sub.add_parser("show", help="the plan: the new commits, and the changes each takes")
    shown.add_argument("--json", action="store_true", help="as JSON")
    commit = sub.add_parser("commit", help="add, remove, move or reword a new commit")
    commit_sub = commit.add_subparsers(dest="action", required=True)
    add = commit_sub.add_parser("add", help="add a commit")
    add.add_argument("message")
    add.add_argument("--after", type=int, metavar="N", help="after commit N (0: first; by default, last)")
    remove = commit_sub.add_parser("remove", help="remove a commit; its changes become unassigned")
    remove.add_argument("number", type=int)
    move = commit_sub.add_parser("move", help="move commit NUMBER to be commit TO")
    move.add_argument("number", type=int)
    move.add_argument("to", type=int)
    reword = commit_sub.add_parser("reword", help="change a commit's message")
    reword.add_argument("number", type=int)
    reword.add_argument("message")
    assign = sub.add_parser("assign", help="have commit NUMBER take the targets' changes")
    assign.add_argument("number", type=int)
    assign.add_argument("targets", nargs="+", metavar="TARGET")
    unassign = sub.add_parser("unassign", help="unassign the targets' changes")
    unassign.add_argument("targets", nargs="+", metavar="TARGET")
    edit = sub.add_parser(
        "edit", help="make PATH in commit NUMBER what's on stdin, by hand (later commits carry the edit)"
    )
    edit.add_argument("number", type=int)
    edit.add_argument("path")
    drop = sub.add_parser("drop-edit", help="drop commit NUMBER's edit of PATH")
    drop.add_argument("number", type=int)
    drop.add_argument("path")
    sub.add_parser("clear", help="remove every commit, to start from scratch")
    sub.add_parser("restore", help="go back to the commits being rebuilt, as they were at the start")
    write = sub.add_parser("write", help="make the new commits, and move the branch to them")
    write.add_argument("--validate", metavar="COMMAND", help="first run COMMAND on each new commit; stop if it fails")
    sub.add_parser("abort", help="stop, without changing any commits")
    tui = sub.add_parser("tui", help="carry on in the TUI (quitting it keeps the plan)")
    tui.add_argument("--validate", metavar="COMMAND", help="as for writing")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rest, directory = list(argv), "."
    while len(rest) >= 2 and rest[0] == "-C":
        directory, rest = rest[1], rest[2:]
    if rest and rest[0] in SUBCOMMANDS:
        args = subcommand_parser().parse_args(rest)
        return _report(lambda: run_subcommand(GitClient(directory), args))
    p = parser()
    args = p.parse_args(argv)
    if (args.range is None) == (args.edit is None):
        p.error("give either a RANGE or --edit COMMIT")
    if args.edit is not None and args.validate is not None:
        p.error("--validate only applies to rebuilding a RANGE")
    git = GitClient(args.directory)
    if args.edit is not None:
        return _report(lambda: 0 if edit_commit(git, args.edit) else 1)
    return _report(lambda: recommit(git, args.range, args.validate))


def _report(run: Callable[[], int]) -> int:
    try:
        return run()
    except RecommitError as e:
        print(f"git-recommit: {e}", file=sys.stderr)
        return 1


def run_subcommand(git: GitClient, args: argparse.Namespace) -> int:
    if args.command == "start":
        if planfile.exists(git):
            raise RecommitError("a recommit is already in progress; carry on with it, or `git recommit abort` it")
        session = start(git, args.range)
        planfile.save(session)
        print("\n".join(show.summary(session)))
        return 0
    if args.command == "abort":
        if not planfile.exists(git):
            raise RecommitError("there's no recommit in progress")
        planfile.remove(git)
        print("Aborted the recommit; no commits were changed.")
        return 0
    session = load_plan(git)
    if args.command == "show":
        if args.json:
            print(json.dumps(show.as_json(session), indent=1))
        else:
            print("\n".join([*show.summary(session), "", *show.diff(session)]))
        return 0
    if args.command == "write":
        write(session, args.validate)
        planfile.remove(git)
        return 0
    if args.command == "tui":
        return tui(session, args.validate)
    result = command(args).run(session)
    planfile.save(session)
    print(result.message)
    return 0


def load_plan(git: GitClient) -> Session:
    """The recommit in progress, without edits of files the range no longer
    changes (saying which it dropped)."""
    session = planfile.load(git)
    if dropped := session.drop_stale_edits():
        planfile.save(session)
        for note in dropped:
            print(f"git-recommit: {note}", file=sys.stderr)
    return session


def command(args: argparse.Namespace) -> Command:
    """The command a subcommand that changes the plan stands for."""
    if args.command == "commit":
        match args.action:
            case "add":
                return commands.AddCommit(args.message, args.after)
            case "remove":
                return commands.RemoveCommit(args.number)
            case "move":
                return commands.MoveCommit(args.number, args.to)
            case _:
                return commands.Reword(args.number, args.message)
    match args.command:
        case "assign":
            return commands.Assign(tuple(Target.parse(t) for t in args.targets), args.number)
        case "unassign":
            return commands.Assign(tuple(Target.parse(t) for t in args.targets), None)
        case "edit":
            return commands.Edit(args.number, args.path, sys.stdin.buffer.read())
        case "drop-edit":
            return commands.DropEdit(args.number, args.path)
        case "clear":
            return commands.Clear()
        case _:
            return commands.Restore()


def start(git: GitClient, spec: str) -> Session:
    """A recommit of spec, starting from its commits as they are."""
    session = Session.load(git, spec)
    if not session.files:
        raise RecommitError(f"the commits in {spec} change nothing")
    session.restore_old_commits()
    return session


def write(session: Session, validate_command: str | None) -> None:
    """Build the new commits, validate them (stopping if that fails), and move
    the branch to them."""
    if conflicts := session.conflicts():
        where = ", ".join(f"{c.path} in commit {session.plan.index(c.commit.key) + 1}" for c in conflicts)
        raise RecommitError(f"resolve the conflicts first: {where}")
    shas = session.build()
    if validate_command is not None:
        subjects = [commit.subject for commit in session.plan.commits]
        # Stop at the first failure (as if the user chose to edit).
        outcome = validate.run(session.git, validate_command, list(zip(shas, subjects, strict=True)), ask=lambda _: "e")
        if outcome.action != "ok":
            assert outcome.index is not None
            raise RecommitError(
                f"validation failed for commit {outcome.index + 1} ({subjects[outcome.index]}); nothing was changed"
            )
    finish(session, shas)


def finish(session: Session, shas: list[str]) -> None:
    session.finish(shas)
    print(f"Rebuilt {len(session.old_commits)} commits as {len(shas)}.")
    if session.old_commits:
        print("The old ones were:")
        for line in session.old_commits:
            print(f"  {line}")
    # The working tree is untouched, so moving HEAD back undoes it all.
    print(f"To undo, run: git reset {session.head[:12]}")
    if left := session.left_uncommitted():
        print("These files have uncommitted changes the new commits don't have:")
        for path in left:
            print(f"  {path}")


def tui(session: Session, validate_command: str | None) -> int:
    """Carry on with a recommit in progress in the TUI; quitting keeps it."""
    from git_recommit.tui import RecommitApp, Terminal

    shas = RecommitApp(session, Terminal(session.git, validate_command)).run()
    if shas is None:
        planfile.save(session)
        print("Kept the plan; carry on with `git recommit tui`, or the other subcommands.")
        return 0
    finish(session, shas)
    planfile.remove(session.git)
    return 0


def recommit(git: GitClient, spec: str, validate_command: str | None) -> int:
    # Textual is slow to import; only load it when it's needed.
    from git_recommit.tui import RecommitApp, Terminal

    session = start(git, spec)
    shas = RecommitApp(session, Terminal(git, validate_command)).run()
    if shas is None:
        print("git-recommit: nothing changed")
        return 1
    finish(session, shas)
    return 0
