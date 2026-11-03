#!/usr/bin/env bash
# EN-2: SPEC.md must be committed before any implementation code.
#
# The root commit is the DVT template, which already contains the SPEC.md template and the README
# stubs, so it does not count. Needs full history: use fetch-depth 0 in CI.
set -euo pipefail

root=$(git rev-list --max-parents=0 HEAD | tail -n 1)

implementation_commits=$(git log --format=%H "$root..HEAD" -- \
  mcp-server agent apps host eval/harness \
  ':(exclude)mcp-server/README.md' ':(exclude)agent/README.md' ':(exclude)apps/README.md' \
  ':(exclude)host/README.md' ':(exclude)eval/harness/README.md')

if [ -z "$implementation_commits" ]; then
  echo "check_spec_first: OK (no implementation commits yet)"
  exit 0
fi

# sed, not head: head closes the pipe early and pipefail would stop the script.
first_spec_commit=$(git log --reverse --format=%H "$root..HEAD" -- SPEC.md | sed -n 1p)

if [ -z "$first_spec_commit" ]; then
  echo "check_spec_first: FAILED. Implementation code exists, but SPEC.md was never changed." >&2
  exit 1
fi

failed=0
for commit in $implementation_commits; do
  if [ "$commit" = "$first_spec_commit" ] || ! git merge-base --is-ancestor "$first_spec_commit" "$commit"; then
    echo "check_spec_first: FAILED. Commit $commit has implementation code, but is not after the first SPEC.md commit $first_spec_commit." >&2
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  exit 1
fi
echo "check_spec_first: OK (first SPEC.md commit: $first_spec_commit)"
