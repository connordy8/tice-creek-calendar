#!/usr/bin/env bash
# Commit and push autobook_state.json (CI only). If another push got in
# first, start from main and merge our booking records into its copy.
# Used by autobook.yml's last step, and by autobook.py right after a
# booking during the long morning watch. Triggers a calendar sync when a
# booking result changed, so "✅ booked" shows up promptly.
# Exits 0 only when main has every local state commit.
set -u
[ -f autobook_state.json ] || exit 0
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
msg="Auto-book update [$(date -u +'%Y-%m-%d %H:%M UTC')]"
git add autobook_state.json
git diff --cached --quiet || git commit -q -m "$msg"
# Nothing waiting to go up (including commits an earlier call couldn't
# push): done
if [ -z "$(git rev-list origin/main..HEAD 2>/dev/null)" ]; then
  exit 0
fi
results_changed=$(git diff origin/main -- autobook_state.json | grep -c '^[+-].*"result"' || true)
ours=$(mktemp)
cp autobook_state.json "$ours"
pushed=""
for i in 1 2 3 4 5 6; do
  if git pull -q --rebase && git push -q; then
    pushed=1
    break
  fi
  git rebase --abort 2>/dev/null || true
  # GitHub unreachable: try again shortly
  if ! git fetch -q origin main; then
    sleep $((i * 10))
    continue
  fi
  git reset -q --hard origin/main
  if ! python3 autobook.py --merge-state "$ours"; then
    # Never fall through to "pushed" without our records
    cp "$ours" autobook_state.json
    echo "::error::Couldn't merge autobook_state.json with main"
    exit 1
  fi
  git add autobook_state.json
  git diff --cached --quiet || git commit -q -m "$msg"
  [ -z "$(git rev-list origin/main..HEAD)" ] && { pushed=1; break; }
  sleep $((i * 5))
done
if [ -z "$pushed" ]; then
  cp "$ours" autobook_state.json
  echo "::error::Couldn't push autobook_state.json"
  exit 1
fi
echo "Pushed booking state"
if [ "$results_changed" != "0" ] && [ -n "${GH_TOKEN:-}" ]; then
  gh workflow run sync.yml --repo "$GITHUB_REPOSITORY" || echo "couldn't trigger sync"
fi
