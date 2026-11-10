#!/usr/bin/env python3
"""Formal hosts: disposable-source management lifecycle and real pinned JDBC driver."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import tempfile
import time
import uuid

from acceptance.fixtures import PASSWORD, Sources, validate_extensions
from acceptance.pg import Client

spec = importlib.util.spec_from_file_location('federation', Path(__file__).with_name('verify-federation.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
ROOT = Path(__file__).resolve().parents[1]


def wait(api, path, field, terminal):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        value = f.get(api, path)
        if value[field] in terminal:
            return value
        time.sleep(0.025)
    raise AssertionError('workflow timeout: ' + path)


def operation(api, method, path, body, key=None, expected='Succeeded'):
    key = key or uuid.uuid4().hex
    status, receipt = f.http(api, method, path, body, key)
    assert status == 202, (method, path, status, receipt.get('code'))
    result = wait(api, 'operations/' + receipt['id'], 'status', ('Succeeded', 'Failed', 'Interrupted'))
    assert result['status'] == expected, (path, result['status'])
    print(f'{method} {path}: {expected}', flush=True)
    return receipt, result


def change(api, identity, action, expected='Succeeded'):
    source = f.get(api, 'sources/' + identity)
    return operation(api, 'POST', f'sources/{identity}/{action}', {'target': f.target(source)}, expected=expected)


def discover(api, connection, databases):
    path = 'discovery/' + str(uuid.uuid4())
    body = {'issued_at': int(time.time()), 'connection': connection}
    status, _ = f.http(api, 'POST', path, body)
    assert status == 202
    value = wait(api, path, 'status', ('succeeded', 'failed'))
    assert value['status'] == 'succeeded'
    candidates = {item['database']: item for item in value['candidates']}
    for database in databases:
        assert candidates[database]['status'] == 'available'
    assert any(item['status'] == 'excluded' for item in candidates.values())
    assert f.http(api, 'POST', path, body)[1] == value
    altered = dict(body, connection=dict(connection, id='conflict'))
    status, conflict = f.http(api, 'POST', path, altered)
    assert status == 409 and conflict['code'] == '40001'


def definition(identity, connection, database, virtual, schema):
    return {'id': identity, 'kind': connection['kind'], 'catalog_alias': 'remote_' + identity,
            'secret_ref': connection['secret_ref'], 'organization': {
                'connection_id': connection['id'], 'instance': connection['instance'],
                'database': database, 'virtual_database': virtual, 'owner': 'fedengine', 'connect': ['reader']},
            'allowed_schemas': [schema], 'default_schema': schema, 'read_only': True, 'ddl_enabled': False}


def browse(db, path):
    return f'browser/{path}?database={db["config"]["name"]}&oid={db["oid"]}&revision={db["revision"]}'


def query(api, db, sql, rows=None):
    path = 'browser/queries/' + str(uuid.uuid4())
    body = {'database': db['config']['name'], 'oid': db['oid'], 'revision': db['revision'],
            'sql': sql, 'issued_at': int(time.time())}
    assert f.http(api, 'POST', path, body)[0] == 200
    value = wait(api, path, 'status', ('succeeded', 'failed', 'canceled'))
    assert value['status'] == 'succeeded', 'browser query failed'
    if rows is not None:
        assert value['result']['rows'] == rows and not value['result']['truncated']
    assert f.http(api, 'POST', path, body)[1] == value
    status, conflict = f.http(api, 'POST', path, dict(body, sql='SELECT 999'))
    assert status == 409 and conflict['code'] == '40001'


def detail(api, db):
    return f.get(api, browse(db, 'object') + '&schema=sales&object=items')


def history(api):
    first = f.get(api, 'operations?offset=0&limit=10')
    entries = []
    for offset in range(0, first['total'], 10):
        page = f.get(api, f'operations?offset={offset}&limit=10')
        assert page['total'] == first['total'] and page['offset'] == offset
        entries.extend(page['items'])
    assert len(entries) == first['total'] and len({item['id'] for item in entries}) == len(entries)
    return {item['id']: item for item in entries}


def durable_databases(databases):
    # A source can publish a new snapshot while reopening. Projection pointers
    # are not object identities; retain every identity/configuration field.
    return [dict(db, schemas=[{key: value for key, value in schema.items() if key != 'snapshot_id'}
                              for schema in db['schemas']]) for db in databases]


def jdbc(address, jar, lock):
    probe = ROOT.parent / 'modorch-fedengine/tests/jdbc/ServerCatalogProbe.java'
    environment = dict(os.environ, JDBC_USER='fedengine', JDBC_PASSWORD=f.smoke.PASSWORD)
    result = subprocess.run([
        'docker', 'run', '--rm', '--network', 'host', '--env', 'JDBC_USER', '--env', 'JDBC_PASSWORD',
        '--mount', f'type=bind,src={jar},dst=/client/driver.jar,readonly',
        '--mount', f'type=bind,src={probe},dst=/client/ServerCatalogProbe.java,readonly',
        lock['jdk_image'], 'java', '--class-path', '/client/driver.jar', '/client/ServerCatalogProbe.java',
        f'jdbc:postgresql://{address[0]}:{address[1]}', 'fedengine/public', 'pg_crm/public,sales',
        'mysql_erp/public,erp', 'mysql_app/public,workflow_app'],
        env=environment, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout + result.stderr).replace(f.smoke.PASSWORD, '[redacted]')
    assert '"virtual_server_contract":"passed"' in result.stdout
    print(result.stdout, end='', flush=True)


def workflow(binary, combined, extensions, library, jar, lock, sources):
    with tempfile.TemporaryDirectory(prefix='mdh-workflow-') as temporary:
        directory = Path(temporary)
        config = f.configuration(directory, combined, extensions)
        file = directory / 'fedengine.toml'
        text = file.read_text().split('\n[[datasources]]', 1)[0].replace('allow_cross_database = true', 'allow_cross_database = false')
        for identity in ['pg_connection', 'mysql_connection']:
            text += f'\n[[grants]]\nprincipal="fedengine"\nsource="{identity}"\nschema="*"\nobject="*"\nactions=["SELECT"]\n'
        file.write_text(text)
        env = dict(os.environ, FEDENGINE_PASSWORD=f.smoke.PASSWORD, MDH_READER_PASSWORD=f.smoke.PASSWORD,
                   WORKFLOW_PG_CREDENTIALS=json.dumps({'username': 'fedengine', 'password': PASSWORD, 'options': {'sslmode': 'disable'}}),
                   WORKFLOW_MYSQL_CREDENTIALS=json.dumps({'username': 'root', 'password': PASSWORD}), RUST_LOG='info')
        env['LD_LIBRARY_PATH'] = str(library) + os.pathsep + env.get('LD_LIBRARY_PATH', '')
        connections = {
            'pg': {'id': 'pg_connection', 'kind': 'postgres', 'instance': {'host': '127.0.0.1', 'port': sources.pg_address[1]}, 'secret_ref': 'WORKFLOW_PG_CREDENTIALS'},
            'mysql': {'id': 'mysql_connection', 'kind': 'mysql', 'instance': {'host': '127.0.0.1', 'port': sources.mysql_port}, 'secret_ref': 'WORKFLOW_MYSQL_CREDENTIALS'},
        }
        process = f.smoke.Process(binary, config, '/', env)
        try:
            listeners = process.ready()
            api = listeners['management']
            with (f.MQTTLoad(listeners['mqtt']) if combined else f.NoMQTT()) as load:
                assert f.get(api, 'sources') == []
                discover(api, connections['pg'], ['crm'])
                discover(api, connections['mysql'], ['erp', 'workflow_app'])
                assert f.get(api, 'sources') == []
                assert [d['config']['name'] for d in f.get(api, 'virtual-databases')] == ['fedengine']
                definitions = [definition(identity, connections[kind], native, virtual, schema) for identity, kind, native, virtual, schema in [
                    ('pg_sales', 'pg', 'crm', 'pg_crm', 'sales'), ('pg_public', 'pg', 'crm', 'pg_crm', 'public'),
                    ('pg_overlap', 'pg', 'crm', 'pg_crm', 'sales'), ('mysql_erp', 'mysql', 'erp', 'mysql_erp', 'erp'),
                    ('mysql_app', 'mysql', 'workflow_app', 'mysql_app', 'workflow_app')]]
                replay = None
                for body in definitions:
                    identity = body['id']
                    key = 'register-' + identity
                    receipt, _ = operation(api, 'POST', 'sources', body, key)
                    assert f.http(api, 'POST', 'sources', body, key)[1]['id'] == receipt['id']
                    assert f.get(api, 'sources/' + identity)['applied_revision'] is None
                    assert 'remote_' + identity not in {d['config']['name'] for d in f.get(api, 'virtual-databases')}
                    change(api, identity, 'probe')
                    _, found = change(api, identity, 'discover')
                    assert body['default_schema'] in found['result_schemas']
                    before = f.get(api, 'sources/' + identity)
                    patch = {'target': f.target(before), 'secret_ref': body['secret_ref']}
                    operation(api, 'PATCH', 'sources/' + identity, patch)
                    stale = {'target': dict(f.target(before), expected_revision=0)}
                    assert f.http(api, 'POST', f'sources/{identity}/apply', stale, uuid.uuid4().hex)[0] == 409
                    source = f.get(api, 'sources/' + identity)
                    operation(api, 'PUT', f'sources/{identity}/schemas', {
                        'target': f.target(source), 'bindings': [{'remote_schema': body['default_schema'], 'enabled': True}],
                        'default_schema': body['default_schema']})
                    change(api, identity, 'apply')
                    assert f.get(api, 'sources/' + identity)['status'] == 'Active'
                    if replay is None:
                        replay = (body, key, receipt['id'])
                    load.checkpoint()
                bad = dict(definitions[0], id='bad_target', catalog_alias='remote_bad', secret_ref='WORKFLOW_MISSING_SECRET')
                operation(api, 'POST', 'sources', bad)
                change(api, 'bad_target', 'probe', expected='Failed')
                change(api, 'bad_target', 'apply', expected='Failed')
                assert f.get(api, 'sources/bad_target')['status'] == 'Degraded'
                databases = {d['config']['name']: d for d in f.get(api, 'virtual-databases')}
                assert set(databases) == {'fedengine', 'pg_crm', 'mysql_erp', 'mysql_app'}
                db = databases['pg_crm']
                schemas = f.get(api, browse(db, 'schemas'))
                assert {s['name'] for s in schemas['schemas']} == {'public', 'sales'}
                facts = detail(api, db)
                assert len(facts['contributors']) == 2 and len(facts['object']['columns']) == 2
                assert facts['source']['schema'] == 'sales'
                objects = f.get(api, browse(db, 'objects') + '&schema=sales&stamp=' + schemas['stamp'])
                assert len(objects['objects']) == 50 and objects['has_more']
                second = f.get(api, browse(db, 'objects') + '&schema=sales&offset=50&stamp=' + schemas['stamp'])
                assert len(second['objects']) == 1 and not second['has_more']
                query(api, db, 'SELECT a.id,b.label FROM sales.items a JOIN public.items b ON a.id=b.id', [['1', 'public']])
                for virtual, schema, label in [('pg_crm', 'sales', 'sales'), ('mysql_erp', 'erp', 'erp'), ('mysql_app', 'workflow_app', 'app')]:
                    with Client(listeners['pgwire'], database=virtual) as client:
                        assert client.query(f'SELECT label FROM {schema}.items') == [[label]]
                    query(api, databases[virtual], f'SELECT label FROM {schema}.items', [[label]])
                with Client(listeners['pgwire'], database='pg_crm', user='reader') as client:
                    f.expect_state(lambda: client.query('SELECT * FROM sales.items'), '42501')
                jdbc(listeners['pgwire'], jar, lock)
                print(f'test MDH-JDBC-{binary.name} ... ok', flush=True)
                # Cancel a real query and wait for the retained worker before publishing changes.
                path = 'browser/queries/' + str(uuid.uuid4())
                body = {'database': db['config']['name'], 'oid': db['oid'], 'revision': db['revision'],
                        'sql': 'SELECT sum(a.i*b.j) FROM range(1000000000) a(i), range(1000000000) b(j)', 'issued_at': int(time.time())}
                assert f.http(api, 'POST', path, body)[0] == 200
                assert f.http(api, 'POST', path + '/cancel', {})[0] == 200
                canceled = wait(api, path, 'status', ('succeeded', 'failed', 'canceled'))
                assert canceled['status'] == 'canceled'
                assert canceled['error']['code'] == '57014'
                change(api, 'pg_sales', 'refresh')
                change(api, 'pg_sales', 'disable')
                current = f.get(api, 'virtual-databases/pg_crm')
                assert current['oid'] == db['oid'] and detail(api, current)['oid'] == facts['oid']
                assert len(detail(api, current)['contributors']) == 1
                status, error = f.http(api, 'GET', browse(dict(current, revision=0), 'schemas'))
                assert status == 409 and error['code'] == '40001'
                query(api, current, 'SELECT label FROM sales.items', [['sales']])
                change(api, 'pg_overlap', 'disable')
                current = f.get(api, 'virtual-databases/pg_crm')
                assert 'sales' not in {s['name'] for s in f.get(api, browse(current, 'schemas'))['schemas']}
                with Client(listeners['pgwire'], database='pg_crm') as client:
                    f.expect_state(lambda: client.query('SELECT * FROM sales.items'), '42P01')
                change(api, 'pg_sales', 'apply')
                current = f.get(api, 'virtual-databases/pg_crm')
                assert detail(api, current)['oid'] == facts['oid']
                source = f.get(api, 'sources/pg_overlap')
                operation(api, 'DELETE', 'sources/pg_overlap', {'target': f.target(source)})
                snapshot = {s['id']: (s['incarnation'], s['desired_revision']) for s in f.get(api, 'sources')}
                dbs_before = durable_databases(f.get(api, 'virtual-databases'))
                before_history = history(api)
                load.checkpoint()
            process.stop(signal.SIGTERM)
            process.close()
            process = f.smoke.Process(binary, config, '/', env)
            listeners = process.ready()
            api = listeners['management']
            with (f.MQTTLoad(listeners['mqtt']) if combined else f.NoMQTT()) as load:
                assert {s['id']: (s['incarnation'], s['desired_revision']) for s in f.get(api, 'sources')} == snapshot
                dbs_after = durable_databases(f.get(api, 'virtual-databases'))
                assert dbs_after == dbs_before, ('durable database identity/configuration changed', dbs_before, dbs_after)
                assert detail(api, f.get(api, 'virtual-databases/pg_crm'))['oid'] == facts['oid']
                after_history = history(api)
                assert {key: after_history.get(key) for key in before_history} == before_history
                # Startup deliberately records new read-only attachment recovery,
                # independently of old user receipts. It does not replay SQL writes.
                recovery = [value for key, value in after_history.items() if key not in before_history]
                assert len(recovery) == len(snapshot)
                assert {item['action']['id'] for item in recovery} == set(snapshot)
                for item in recovery:
                    identity = item['action']['id']
                    assert item['idempotency_key'].startswith('recovery:')
                    assert item['parameter_digest'] == 'startup-reconciliation' and item['action']['action'] == 'apply'
                    assert (item['incarnation'], item['target_revision']) == snapshot[identity]
                    assert item['status'] == ('Failed' if identity == 'bad_target' else 'Succeeded')
                assert f.http(api, 'POST', 'sources', replay[0], replay[1])[1]['id'] == replay[2]
                query(api, f.get(api, 'virtual-databases/pg_crm'), 'SELECT label FROM sales.items', [['sales']])
                for source in f.get(api, 'sources'):
                    identity = source['id']
                    change(api, identity, 'disable')
                    source = f.get(api, 'sources/' + identity)
                    operation(api, 'DELETE', 'sources/' + identity, {'target': f.target(source)})
                    load.checkpoint()
                assert f.get(api, 'sources') == []
                assert [d['config']['name'] for d in f.get(api, 'virtual-databases')] == ['fedengine']
                with sources.pg() as native:
                    assert native.query('SELECT label FROM sales.items') == [['sales']]
                assert sources.mysql('SELECT label FROM workflow_app.items') == 'app'
            process.stop(signal.SIGINT)
        except BaseException:
            diagnostics = ''.join(process.lines)[-6000:]
            for secret in [PASSWORD, f.smoke.PASSWORD, env['WORKFLOW_PG_CREDENTIALS'], env['WORKFLOW_MYSQL_CREDENTIALS']]:
                diagnostics = diagnostics.replace(secret, '[redacted]')
            print(diagnostics, flush=True)
            raise
        finally:
            process.close()
    print(f'test MDH-WORKFLOW-{binary.name} ... ok', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, required=True)
    parser.add_argument('--extensions-dir', type=Path, required=True)
    parser.add_argument('--duckdb-library-dir', type=Path, required=True)
    parser.add_argument('--jdbc-jar', type=Path, required=True)
    args = parser.parse_args()
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    lock = json.loads((ROOT.parent / 'modorch-fedengine/tests/fixtures/metadata/clients.json').read_text())
    jar = args.jdbc_jar.resolve()
    assert hashlib.sha256(jar.read_bytes()).hexdigest() == lock['artifacts']['postgresql-' + lock['jdbc_version'] + '.jar'], 'JDBC checksum mismatch'
    extensions = validate_extensions(args.extensions_dir)
    print(json.dumps({'platform': platform.platform(), 'jdbc_version': lock['jdbc_version'],
                      'jdbc_sha256': hashlib.sha256(jar.read_bytes()).hexdigest(), 'jdk_image': lock['jdk_image'],
                      'binaries_sha256': {name: hashlib.sha256((args.bin_dir / name).read_bytes()).hexdigest()
                                          for name in ['fedengine-server', 'datahub-server']},
                      'remote_reproducibility': 'NotRun'}), flush=True)
    with Sources() as sources:
        with sources.pg() as native:
            native.query("CREATE SCHEMA sales; CREATE TABLE sales.items(id INTEGER PRIMARY KEY,label TEXT); INSERT INTO sales.items VALUES(1,'sales'); CREATE TABLE public.items(id INTEGER PRIMARY KEY,label TEXT); INSERT INTO public.items VALUES(1,'public')")
            for index in range(50):
                native.query(f'CREATE TABLE sales.page_{index:02d}(id INTEGER)')
        sources.mysql("CREATE TABLE erp.items(id INTEGER PRIMARY KEY,label VARCHAR(32)); INSERT INTO erp.items VALUES(1,'erp'); CREATE DATABASE workflow_app; CREATE TABLE workflow_app.items(id INTEGER PRIMARY KEY,label VARCHAR(32)); INSERT INTO workflow_app.items VALUES(1,'app')")
        for combined in (False, True):
            name = 'datahub-server' if combined else 'fedengine-server'
            workflow(args.bin_dir.resolve() / name, combined, extensions, args.duckdb_library_dir.resolve(), jar, lock, sources)


if __name__ == '__main__':
    main()
