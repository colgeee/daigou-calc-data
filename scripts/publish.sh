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

# Hard-kill recovery: a run killed between `worktree add` and `cleanup` leaves gh-pages
# checked out in a worktree at *that* run's own mktemp path -- a path this run has no way
# to predict, so matching against this run's own freshly-minted $work (as an earlier
# version of this script did) can never find it: $work is unique per run by construction,
# so it never collides with anything a prior run registered. Only one worktree can have a
# given branch checked out at a time, so the reliable match is by branch, not by path: walk
# `git worktree list --porcelain` for the entry (if any) whose `branch` line is
# `refs/heads/gh-pages`, or whose recorded path no longer exists on disk (a directory `git
# worktree prune` above did not catch, e.g. because it was recreated by something else), and
# force-remove it. This must run before `git branch --force gh-pages FETCH_HEAD` below,
# since git refuses to force-move a branch that is checked out in another worktree.
cur_path=""
cur_branch=""
reap_if_stale() {
  if [ -n "$cur_path" ] && { [ "$cur_branch" = "refs/heads/gh-pages" ] || [ ! -d "$cur_path" ]; }; then
    echo "publish: removing a stale worktree registration at $cur_path"
    git worktree remove --force "$cur_path" 2>/dev/null || rm -rf "$cur_path"
  fi
}
while IFS= read -r line; do
  case "$line" in
    "worktree "*)
      reap_if_stale
      cur_path="${line#worktree }"
      cur_branch=""
      ;;
    "branch "*)
      cur_branch="${line#branch }"
      ;;
  esac
done < <(git worktree list --porcelain)
reap_if_stale
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
