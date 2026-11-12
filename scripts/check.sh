#!/bin/sh
# Local source validation. No remote publication or destructive business data I/O.
set -eu
cd "$(dirname "$0")/.."
results=$(mktemp /tmp/mdh-local-contracts.XXXXXX)
trap 'rm -f "$results"' EXIT HUP INT TERM
python3 scripts/verify-dependency-identity.py >"$results"
cat "$results"
python3 -m unittest discover -s tests -p 'test_*.py' -v
./scripts/check-mqtt-validation.sh
cargo fmt --all -- --check
cargo check --workspace --all-targets --locked --offline
cargo clippy --workspace --all-targets --locked --offline -- -D warnings
if cargo test --workspace --locked --offline >>"$results" 2>&1; then
    cat "$results"
else
    cat "$results"
    exit 1
fi
python3 scripts/check-contracts.py --scope local --results "$results"
RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps --locked --offline
# Deliberately build each normal dependency closure independently.
cargo build -p mqtt-server --locked --offline
cargo build -p fedengine-server --locked --offline
cargo build -p datahub-server --locked --offline
python3 scripts/smoke-services.py
python3 scripts/verify-mqtt-service.py
