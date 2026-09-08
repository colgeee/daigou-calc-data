#!/usr/bin/env bash
# Publish out/v1/* to the root of the gh-pages branch. Usage: scripts/publish.sh "<commit message>"
#
# Idempotent: it commits only when a published file actually changed, and it copies over a
# checkout of gh-pages, so files that this build did not produce (e.g. the quarterly
# rates file during a daily fx run) are carried forward untouched.
set -euo pipefail

msg="${1:-publish}"
cd "$(git rev-parse --show-toplevel)"

if [ ! -d out/v1 ]; then
  echo "publish: out/v1 is missing -- build first (python -m pipeline rates / python -m pipeline fx)" >&2
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

git worktree add --quiet "$work" gh-pages
mkdir -p "$work/v1"
cp out/v1/* "$work/v1/"
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
