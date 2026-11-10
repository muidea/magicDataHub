#!/bin/sh
# Both formal hosts and pinned JDBC must actually succeed.
set -eu
cd "$(dirname "$0")/.."
results=$(mktemp /tmp/mdh-workflow-contracts.XXXXXX)
trap 'rm -f "$results"' EXIT HUP INT TERM
if python3 scripts/verify-management-workflow.py "$@" >"$results" 2>&1; then
    cat "$results"
else
    cat "$results"
    exit 1
fi
python3 scripts/check-contracts.py --scope workflow --results "$results"
