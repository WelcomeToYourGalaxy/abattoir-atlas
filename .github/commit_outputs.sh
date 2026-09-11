#!/usr/bin/env bash
# Commit generated build outputs, last-write-wins.
#
# These files are derived, not authored: facilities.json.gz, the map, the
# reports. Git cannot merge a gzip, so a plain `pull --rebase` stops on a binary
# conflict and every retry then fails on the unmerged state. Rebasing is the
# wrong tool here anyway -- there is nothing to reconcile, because the newest
# build supersedes whatever was there.
#
# So: stash what this run produced, hard-reset onto origin, put it back, commit.
#
# Usage: bash .github/commit_outputs.sh "commit message" path [path...]
set -uo pipefail

msg="$1"; shift
paths=("$@")

git config user.name  "atlas-bot"
git config user.email "atlas-bot@users.noreply.github.com"

for attempt in 1 2 3; do
  tmp=$(mktemp -d)
  for p in "${paths[@]}"; do
    if [ -e "$p" ]; then
      mkdir -p "$tmp/$(dirname "$p")"
      cp -r "$p" "$tmp/$(dirname "$p")/"
    fi
  done

  git rebase --abort 2>/dev/null || true
  git reset --hard HEAD --quiet
  git fetch origin "$GITHUB_REF_NAME" --quiet
  # Reset to FETCH_HEAD, not to the remote-tracking ref. A single-branch fetch
  # does not always move origin/<branch>, and resetting to a stale tracking ref
  # silently discards whatever another workflow pushed in the meantime.
  git reset --hard FETCH_HEAD --quiet

  for p in "${paths[@]}"; do
    if [ -e "$tmp/$p" ]; then
      mkdir -p "$(dirname "$p")"
      cp -r "$tmp/$p" "$(dirname "$p")/"
    fi
  done
  rm -rf "$tmp"

  git add -f "${paths[@]}" 2>/dev/null || true
  if git diff --staged --quiet; then
    echo "no change"
    exit 0
  fi
  git commit -q -m "$msg"
  if git push --quiet; then
    echo "pushed: $msg"
    exit 0
  fi
  echo "push race; retry $attempt"
  sleep $((attempt * 5))
done

echo "could not push after 3 attempts" >&2
exit 1
