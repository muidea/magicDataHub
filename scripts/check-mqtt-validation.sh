#!/bin/sh
# Source-only MQTT tool checks; external clients and Docker are not required.
set -eu
cd "$(dirname "$0")/../tools/mqtt-broker-validation"
python3 -B - <<'PY'
from pathlib import Path
for path in Path('.').rglob('*.py'):
    if not any(part in {'.venv', '__pycache__', 'results'} for part in path.parts):
        compile(path.read_bytes(), str(path), 'exec')
PY
python3 -B run.py --check
python3 -B upstream_audit.py
python3 -B normative_map.py
python3 -B oracle_sufficiency.py
python3 -B -m unittest discover -s tests -v
