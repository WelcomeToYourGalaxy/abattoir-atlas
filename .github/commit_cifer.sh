#!/usr/bin/env bash
# Shared commit step for the CIFER workflows. Rebases and retries, because
# another workflow may land on main while this one runs.
set -uo pipefail
git config user.name  "atlas-bot"
git config user.email "atlas-bot@users.noreply.github.com"

[ -f raw/cifer.jsonl ] || { echo "nothing harvested"; exit 0; }
git add -f raw/cifer.jsonl
[ -f work/cifer_checkpoint.json ] && git add -f work/cifer_checkpoint.json
[ -f work/cifer_form.json ] && git add -f work/cifer_form.json
git diff --staged --quiet && { echo "no change"; exit 0; }
git commit -m "cifer: $(wc -l < raw/cifer.jsonl) rows"

for i in 1 2 3; do
  if git pull --rebase --autostash origin "$GITHUB_REF_NAME" && git push; then
    exit 0
  fi
  echo "push race; retry $i"
  sleep $((i * 5))
done
exit 1
