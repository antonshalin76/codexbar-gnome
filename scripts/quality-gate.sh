#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

echo "==> Git diff hygiene"
git diff --check

echo "==> Python syntax"
python3 -m py_compile bin/codexbar-gnome-indicator

echo "==> Python unit tests"
python3 -m unittest discover -s tests -v

echo "==> Shell syntax"
sh -n install.sh
sh -n uninstall.sh
sh -n scripts/install-git-hooks.sh

if command -v shellcheck >/dev/null 2>&1; then
  echo "==> ShellCheck"
  shellcheck install.sh uninstall.sh scripts/install-git-hooks.sh .githooks/pre-commit
else
  echo "==> ShellCheck skipped: shellcheck is not installed"
fi

echo "Quality gate passed"
