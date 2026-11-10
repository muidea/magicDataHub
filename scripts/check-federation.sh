#!/bin/sh
# Explicit opt-in to owned Docker sources; missing prerequisites are errors.
set -eu
cd "$(dirname "$0")/.."
results=$(mktemp /tmp/mdh-remote-contracts.XXXXXX)
trap 'rm -f "$results"' EXIT HUP INT TERM
if python3 scripts/verify-federation.py "$@" >"$results" 2>&1; then
    cat "$results"
else
    cat "$results"
    exit 1
fi
python3 scripts/check-contracts.py --scope remote --results "$results"
