#!/usr/bin/env bash
# Publish the v1 data files to the root of the gh-pages branch.
# Usage: scripts/publish.sh "<commit message>"
#
# Idempotent: it commits only when a published file actually changed, and it copies over a
# checkout of gh-pages, so files that this build did not produce (e.g. the quarterly
# rates file during a daily fx run) are carried forward untouched.
set -euo pipefail

# The exact published set, named rather than globbed: `cp out/v1/*` would publish whatever
# happens to sit in the directory -- a stray `.tmp` from an interrupted write, a renamed
# file from an older build -- and would silently publish nothing at all if the build wrote
# nothing. The fx workflow restores the current rates files into out/v1 before calling
# this, so all three are expected on every run.
FILES=(rates.json.gz rates.json fx.json)

msg="${1:-publish}"
cd "$(git rev-parse --show-toplevel)"

bad=()
for f in "${FILES[@]}"; do
  if [ ! -f "out/v1/$f" ]; then
    bad+=("$f (missing)")
  elif [ ! -s "out/v1/$f" ]; then
    bad+=("$f (0 bytes)")
  fi
done
if [ ${#bad[@]} -ne 0 ]; then
  echo "publish: REFUSING TO PUBLISH -- out/v1 is incomplete: ${bad[*]}" >&2
  echo "publish: build first (python -m pipeline rates / python -m pipeline fx)" >&2
  exit 1
fi

git worktree prune

# Point the local gh-pages branch at the remote one, or bootstrap an empty branch when the
# remote has none yet (the very first publish).
if git fetch --quiet origin gh-pages 2>/dev/null; then
  git branch --force gh-pages FETCH_HEAD
elif ! git rev-parse --verify --quiet refs/heads/gh-pages >/dev/null; then
  empty_tree="$(git hash-object -t tree /dev/null)"
  root="$(git -c user.name=github-actions -c user.email=actions@github.com \
    commit-tree -m 'init gh-pages' "$empty_tree")"
  git branch gh-pages "$root"
fi

parent="$(mktemp -d)"
work="$parent/gh-pages"
cleanup() {
  git worktree remove --force "$work" >/dev/null 2>&1 || true
  rm -rf "$parent"
}
trap cleanup EXIT

# Hard-kill recovery: a run killed between `worktree add` and `cleanup` leaves the path
# registered, and `git worktree add` then refuses with "already exists". `git worktree
# prune` above clears a registration whose directory is gone; one whose directory survived
# needs the explicit removal.
while IFS= read -r p; do
  if [ "$p" = "$work" ] || { [ -d "$p" ] && [ -d "$work" ] && [ "$p" -ef "$work" ]; }; then
    echo "publish: removing a stale worktree registration at $p"
    git worktree remove --force "$p"
  fi
done < <(git worktree list --porcelain | sed -n 's/^worktree //p')

git worktree add --quiet "$work" gh-pages
mkdir -p "$work/v1"
for f in "${FILES[@]}"; do
  cp "out/v1/$f" "$work/v1/$f"
done
cat > "$work/index.html" <<'EOF'
<!doctype html><meta charset="utf-8"><title>daigou-calc-data</title>
<p>Static data for 代購算盤 Daigou Calc: <a href="v1/rates.json.gz">v1/rates.json.gz</a> (quarterly), <a href="v1/fx.json">v1/fx.json</a> (daily). Source: <a href="https://github.com/colgeee/daigou-calc-data">github.com/colgeee/daigou-calc-data</a>.</p>
EOF
touch "$work/.nojekyll"
git -C "$work" add -A
if git -C "$work" diff --cached --quiet; then
  echo "publish: nothing changed"
else
  git -C "$work" -c user.name=github-actions -c user.email=actions@github.com commit -q -m "$msg"
  git -C "$work" push origin gh-pages
  echo "publish: pushed $(git -C "$work" rev-parse --short HEAD) ($msg)"
fi
