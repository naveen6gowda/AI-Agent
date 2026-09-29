#!/usr/bin/env bash
# git_mirror.sh — nightly off-box backup: commit any local changes and push
# main to the private GitHub mirror. Scheduled by sentinel-gitmirror.timer
# (daily 04:15, after the 03:30 checkpoint maintenance).
#
# Design notes:
#   - `git add -A` honors .gitignore, so secrets (.env*), runtime state
#     (var/, *.sqlite) and backups (*.bak*) can never enter a snapshot.
#   - A clean tree with nothing to push exits 0 quietly; any real failure
#     (SSH, rejected push) exits non-zero and OnFailure= pages Telegram.
set -euo pipefail
cd /opt/sentinel

git add -A
if ! git diff --cached --quiet; then
    git commit -m "nightly snapshot $(date +%Y-%m-%d)"
    echo "committed snapshot for $(date +%Y-%m-%d)"
fi

# Lint gate: CI runs `ruff check` first, so a lint slip here would burn a red
# run on GitHub overnight. Fail the mirror instead - OnFailure= pages Telegram
# and the journal below carries ruff's findings into the alert.
if ! .venv/bin/ruff check .; then
    echo "ruff check failed - fix lint on the LXC before the mirror can push" >&2
    exit 1
fi

# Test gate: CI runs pytest next, and a live hotfix can break a test as
# easily as a lint rule. ~3 s, no network (respx mocks every client).
if ! .venv/bin/python -m pytest -q -x -p no:cacheprovider 2>&1 | tail -20; then
    echo "pytest failed - fix the tests on the LXC before the mirror can push" >&2
    exit 1
fi

ahead=$(git rev-list --count '@{upstream}..HEAD')
if [ "$ahead" -gt 0 ]; then
    git push origin main
    echo "pushed $ahead commit(s) to origin/main"
else
    echo "nothing to push — mirror already current"
fi
