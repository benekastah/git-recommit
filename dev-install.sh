#!/bin/sh
# Install git-recommit from this clone, for using it while working on it.
#
# Installs it in editable mode into a virtualenv, so changes to the clone take
# effect without reinstalling, links the `git-recommit` command into
# ~/.local/bin (so `git recommit` works), and makes this repository use the
# hooks in git_hooks/. It also sets up the clone's .venv, which the hooks use,
# with dev-venv.sh. Safe to rerun, e.g. after pyproject.toml changes.

set -eu

clone=$(cd "$(dirname "$0")" && pwd)
venv="$HOME/.local/share/git-recommit/venv"
bin_dir="$HOME/.local/bin"

if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
fi
"$venv/bin/python" -m pip install --quiet --editable "$clone"
echo "installed git-recommit from $clone into $venv"

mkdir -p "$bin_dir"
if [ -e "$bin_dir/git-recommit" ] && [ ! -L "$bin_dir/git-recommit" ]; then
    echo "dev-install: $bin_dir/git-recommit exists and isn't a link; move it out of the way and rerun" >&2
    exit 1
fi
ln -sfn "$venv/bin/git-recommit" "$bin_dir/git-recommit"
echo "linked $bin_dir/git-recommit -> $venv/bin/git-recommit"

"$clone/dev-venv.sh"
echo "set up $clone/.venv with the dev dependencies"
git -C "$clone" config core.hooksPath git_hooks
echo "set this repository's hooks to git_hooks/"

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "warning: $bin_dir isn't on your PATH, so git won't find git recommit; add it in" \
            "your shell's startup file" >&2 ;;
esac
