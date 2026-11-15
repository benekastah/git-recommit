import argparse
import sys

from git_recommit.edit_commit import edit_commit
from git_recommit.errors import RecommitError
from git_recommit.git import GitClient
from git_recommit.session import Session


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git recommit",
        description="Rebuild a run of commits into new ones, choosing in a TUI which changes each new commit takes.",
        epilog="RANGE is <base> (the commits after base, up to HEAD), <a>..<b> or <a>...<b>; e.g. HEAD~6.",
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


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if (args.range is None) == (args.edit is None):
        p.error("give either a RANGE or --edit COMMIT")
    if args.edit is not None and args.validate is not None:
        p.error("--validate only applies to rebuilding a RANGE")
    git = GitClient(args.directory)
    try:
        if args.edit is not None:
            return 0 if edit_commit(git, args.edit) else 1
        return recommit(git, args.range, args.validate)
    except RecommitError as e:
        print(f"git-recommit: {e}", file=sys.stderr)
        return 1


def recommit(git: GitClient, spec: str, validate_command: str | None) -> int:
    # Textual is slow to import; only load it when it's needed.
    from git_recommit.tui import RecommitApp, Terminal

    session = Session.load(git, spec)
    if not session.files:
        raise RecommitError(f"the commits in {spec} change nothing")
    shas = RecommitApp(session, Terminal(git, validate_command)).run()
    if shas is None:
        print("git-recommit: nothing changed")
        return 1
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
    return 0
