"""Owned disposable containers only; never accept a caller's business DSN."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from .pg import Client, PGError

LOCK = json.loads(Path(__file__).with_name('sources.json').read_text())
PASSWORD = 'mdh-disposable-source-only'


def extension_manifest(keys=None):
    selected = list(LOCK['extensions'].keys()) if keys is None else list(keys)
    return {'duckdb_version': LOCK['duckdb_version'], 'platform': 'linux_amd64',
            'extensions': {key: {field: LOCK['extensions'][key][field] for field in ['path', 'sha256']}
                           for key in selected}}


def command(arguments, **kwargs):
    return subprocess.check_output(arguments, text=True, timeout=60, stderr=subprocess.PIPE, **kwargs).strip()


class Sources:
    def __init__(self):
        self.names = []
        self.pg_address = None
        self.mysql_port = None

    def __enter__(self):
        try:
            for kind, port, image, variables in [
                ('pg', 5432, LOCK['postgres_image'], {'POSTGRES_DB': 'crm', 'POSTGRES_USER': 'fedengine', 'POSTGRES_PASSWORD': PASSWORD}),
                ('mysql', 3306, LOCK['mysql_image'], {'MYSQL_DATABASE': 'erp', 'MYSQL_ROOT_PASSWORD': PASSWORD}),
            ]:
                name = 'mdh-acceptance-' + kind + '-' + uuid.uuid4().hex[:12]
                arguments = ['docker', 'run', '--rm', '--detach', '--name', name, '--label', 'magicdatahub.disposable=acceptance', '--publish', f'127.0.0.1::{port}']
                for key, value in variables.items():
                    arguments += ['--env', f'{key}={value}']
                self.names.append(name)
                command(arguments + [image])
                mapped = command(['docker', 'port', name, str(port) + '/tcp'])
                host, mapped_port = mapped.rsplit(':', 1)
                assert host == '127.0.0.1'
                if kind == 'pg':
                    self.pg_address = (host, int(mapped_port))
                    self.pg_name = name
                else:
                    self.mysql_port = int(mapped_port)
                    self.mysql_name = name
            deadline = time.monotonic() + 90
            while True:
                try:
                    with self.pg() as client:
                        assert client.query('SHOW server_version')[0][0].startswith('17.7')
                    assert self.mysql('SELECT VERSION()') == '5.7.44'
                    break
                except (OSError, EOFError, PGError, subprocess.SubprocessError):
                    if time.monotonic() >= deadline:
                        raise AssertionError('owned source startup timeout') from None
                    time.sleep(0.25)
            return self
        except BaseException:
            self.close()
            raise

    def pg(self):
        return Client(self.pg_address, database='crm', password=PASSWORD)

    def mysql(self, sql):
        return command(['docker', 'exec', '--env', 'MYSQL_PWD=' + PASSWORD,
                        self.mysql_name, 'mysql', '--protocol=TCP', '-h127.0.0.1', '-uroot',
                        '--batch', '--skip-column-names', 'erp', '-e', sql])

    def environment(self):
        return {
            'MDH_TEST_PG_DSN': f'host=127.0.0.1 port={self.pg_address[1]} dbname=crm user=fedengine password={PASSWORD}',
            'MDH_TEST_MYSQL_DSN': f'host=127.0.0.1 port={self.mysql_port} database=erp user=root password={PASSWORD}',
        }

    def close(self):
        errors = []
        for name in reversed(self.names):
            result = subprocess.run(['docker', 'rm', '--force', '--volumes', name], capture_output=True, text=True, timeout=30)
            if result.returncode and 'No such container' not in result.stderr:
                errors.append(name)
        self.names.clear()
        if errors:
            raise AssertionError('owned source cleanup failed')

    def __exit__(self, *args):
        self.close()


def validate_extensions(directory, keys=None):
    directory = directory.resolve()
    selected = list(LOCK['extensions'].keys()) if keys is None else list(keys)
    for key in selected:
        extension = LOCK['extensions'][key]
        path = directory / extension['path']
        assert path.is_file() and not path.is_symlink(), 'missing prepared pinned extension'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == extension['sha256'], 'extension checksum mismatch'
    return directory
