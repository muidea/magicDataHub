#!/usr/bin/env python3
"""Validate default local Compose profiles, using only a unique disposable project."""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag-prefix', required=True)
    parser.add_argument('--profile', choices=['dev', 'release'], default='dev')
    args = parser.parse_args()
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    project = 'mdh-compose-check-' + uuid.uuid4().hex[:12]
    command = ['docker', 'compose', '--project-name', project, '--file', str(ROOT / 'docker/compose.yaml')]
    env = dict(os.environ, MDH_IMAGE_PREFIX=args.tag_prefix, MDH_IMAGE_PROFILE=args.profile,
               FEDENGINE_PASSWORD=uuid.uuid4().hex)
    # Never stop an existing instance to free a default development port.
    for port in [1883, 2883, 15432, 15433, 25432, 25433]:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    try:
        subprocess.run(command + ['up', '--detach'], env=env, check=True, timeout=60)
        for mqtt, pg, management in [(1883, 15432, 15433), (2883, 25432, 25433)]:
            deadline = time.monotonic() + 60
            while True:
                probe = subprocess.run([sys.executable, str(ROOT / 'scripts/probe-service.py'),
                                        '--mqtt', f'127.0.0.1:{mqtt}', '--pgwire', f'127.0.0.1:{pg}',
                                        '--management', f'127.0.0.1:{management}'],
                                       env=env, capture_output=True, text=True, timeout=45)
                if probe.returncode == 0:
                    break
                assert time.monotonic() < deadline, 'Compose protocols did not become ready'
                time.sleep(0.1)
            print(probe.stdout.strip(), flush=True)
        ids = subprocess.check_output(command + ['ps', '--quiet'], env=env, text=True).split()
        assert len(ids) == 3
        subprocess.run(command + ['kill', '--signal', 'SIGTERM'], env=env, check=True, timeout=10)
        assert subprocess.check_output(['docker', 'wait', *ids], text=True, timeout=45).split() == ['0', '0', '0']
        for name in ids:
            logs = subprocess.check_output(['docker', 'logs', name], text=True, stderr=subprocess.STDOUT)
            assert 'GracefullyStopped' in logs and env['FEDENGINE_PASSWORD'] not in logs
    finally:
        # Only this random test project and its volumes; never a user's instance.
        subprocess.run(command + ['down', '--volumes'], env=env, check=True, timeout=60)
    print('test MDH-COMPOSE-001 ... ok', flush=True)


if __name__ == '__main__':
    main()
