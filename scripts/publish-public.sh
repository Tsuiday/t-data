#!/bin/sh
# Publish this repo's main branch to the public mirror Tsuiday/t-data with every
# commit's author and committer set to Tsuiday's GitHub no-reply address, so
# personal emails and machine names in the private history never go public.
# The rewrite is deterministic, so each run is a plain fast-forward push.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
WORK=${TMPDIR:-/tmp}/t-data-public
NAME=Tsuiday
EMAIL=305009336+Tsuiday@users.noreply.github.com

rm -rf "$WORK"
git clone -q --no-local --branch main "$SRC" "$WORK"
cd "$WORK"
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter "
export GIT_AUTHOR_NAME='$NAME' GIT_AUTHOR_EMAIL='$EMAIL'
export GIT_COMMITTER_NAME='$NAME' GIT_COMMITTER_EMAIL='$EMAIL'" -- main >/dev/null
if git log main --format='%ae %ce' | grep -qv "^$EMAIL $EMAIL\$"; then
  echo "rewrite incomplete; not pushing" >&2; exit 1
fi
GH_TOKEN=$(gh auth token --user Tsuiday) git push https://github.com/Tsuiday/t-data.git main:main
rm -rf "$WORK"
