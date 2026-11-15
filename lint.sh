#!/bin/sh
# Every check but the tests: shellcheck on the shell scripts, that the Python
# code is formatted (fix it with `.venv/bin/ruff format`), Ruff's linter, and
# the type checker (also as if on Windows, for code only Windows runs).
#
# Uses the tools in .venv, which check.sh (by dev-venv.sh) sets up.

set -eu

cd "$(dirname "$0")"

git ls-files -z '*.sh' 'git_hooks/*' | xargs -0 .venv/bin/shellcheck
.venv/bin/ruff format --check --quiet
.venv/bin/ruff check --quiet
.venv/bin/python -m mypy
.venv/bin/python -m mypy --platform win32
