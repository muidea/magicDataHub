#!/usr/bin/env python3
"""M4/M5 acceptance: actual binaries, owned PG/MySQL and concurrent MQTT traffic."""
import argparse
import concurrent.futures
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from http.client import HTTPConnection

from acceptance.fixtures import LOCK, PASSWORD as SOURCE_PASSWORD, Sources, validate_extensions
from acceptance.pg import Client, PGError


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


smoke = module('smoke', 'smoke-services.py')
mqtt = module('mqtt_acceptance', 'verify-mqtt-service.py')


def expect_state(operation, expected):
    try:
        operation()
    except PGError as error:
        assert error.sqlstate == expected, (error.sqlstate, expected)
        return
    raise AssertionError('expected PostgreSQL rejection ' + expected)


def http(address, method, path, body=None, key=None):
    connection = HTTPConnection(*address, timeout=20)
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Idempotency-Key'] = key
    try:
        connection.request(method, '/api/v1/' + path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read(2 * 1024 * 1024 + 1)
        assert len(data) <= 2 * 1024 * 1024
        return response.status, json.loads(data)
    finally:
        connection.close()


def get(address, path):
    status, value = http(address, 'GET', path)
    assert status == 200, ('GET', path, status)
    return value


def finish(address, receipt):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = get(address, 'operations/' + receipt['id'])
        if result['status'] in ('Succeeded', 'Failed', 'Interrupted'):
            assert result['status'] == 'Succeeded', 'management operation did not succeed'
            return result
        time.sleep(0.025)
    raise AssertionError('management operation timeout')


def target(source):
    return {'incarnation': source['incarnation'], 'expected_revision': source['desired_revision']}


def protocol(address):
    expect_state(lambda: Client(address, password='wrong-test-only'), '28P01')
    with Client(address) as pg:
        assert pg.query('SELECT 42::INTEGER') == [['42']]
        pg.parse('SELECT $1::INTEGER + 1, $2::TEXT', types=(23, 25))
        pg.bind([41, None])
        assert pg.execute() == ([['42', None]], b'C')
        pg.sync()
        pg.parse('SELECT i::INTEGER FROM range(5) t(i)', name='range')
        pg.bind(portal='rows', statement='range')
        for rows, boundary in [([['0'], ['1']], b's'), ([['2'], ['3']], b's'), ([['4']], b'C')]:
            assert pg.execute(2, 'rows') == (rows, boundary)
        pg.sync()
        pg.parse('SELECT i::INTEGER FROM range(1000000) t(i)', name='drop')
        pg.bind(portal='drop', statement='drop')
        assert pg.execute(1, 'drop') == ([['0']], b's')
        pg.close_portal('drop')
        assert pg.query('SELECT 1::INTEGER') == [['1']]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            query = pool.submit(pg.query, 'SELECT sum(a.i*b.j) FROM range(1000000000) a(i), range(1000000000) b(j)')
            deadline = time.monotonic() + 5
            while not query.done() and time.monotonic() < deadline:
                time.sleep(0.05)
                pg.cancel()
            expect_state(lambda: query.result(timeout=5), '57014')
        assert pg.query('SELECT 7::INTEGER') == [['7']]
    print('PG SCRAM, rejected password, parameters/NULL, Portal resume/close, CancelRequest/reuse: Passed', flush=True)


class MQTTLoad:
    def __init__(self, address):
        self.address = address
        self.stop = threading.Event()
        self.progress = threading.Condition()
        self.count = 0
        self.failure = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        clients = []
        try:
            subscriber = mqtt.Client(self.address, 'joint-subscriber')
            publisher = mqtt.Client(self.address, 'joint-publisher')
            clients = [subscriber, publisher]
            subscriber.subscribe('joint/traffic')
            while not self.stop.is_set():
                qos = self.count % 3
                payload = struct.pack('!I', self.count) + b'x' * 1024
                publisher.publish('joint/traffic', payload, qos)
                subscriber.delivery('joint/traffic', payload, qos)
                if self.count % 16 == 0:
                    subscriber.ping()
                with self.progress:
                    self.count += 1
                    self.progress.notify_all()
                self.stop.wait(0.005)
        except BaseException as error:
            self.failure = error
            with self.progress:
                self.progress.notify_all()
        finally:
            for client in clients:
                client.stream.close()

    def checkpoint(self, extra=3):
        with self.progress:
            target = self.count + extra
            success = self.progress.wait_for(lambda: self.count >= target or self.failure is not None, timeout=10)
            assert success and self.failure is None and self.count >= target, 'MQTT traffic stopped progressing'

    def __enter__(self):
        self.thread.start()
        self.checkpoint()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive() and self.failure is None, 'MQTT traffic failed'


class NoMQTT:
    def checkpoint(self, extra=3):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


def configuration(directory, combined, extensions):
    app = 'acceptance-combined' if combined else 'acceptance-fedengine'
    host = f'[application]\nname="{app}"\n[runtime]\nshutdown_timeout_ms=25\nshutdown_retry_ms=5\n'
    if combined:
        host += '[mqtt]\nconfig_file="mqtt.toml"\n'
        (directory / 'mqtt.toml').write_text('[mqtt.core]\nenabled=true\nlistener="127.0.0.1:0"\nmax_connections=4\nmax_sessions=8\n[mqtt.security]\nanonymous_access=true\n')
    host += '[fedengine]\nconfig_file="fedengine.toml"\n'
    (directory / 'host.toml').write_text(host)
    text = '\n'.join(line for line in (smoke.ROOT / 'config/fedengine/combined.toml').read_text().splitlines() if not line.lstrip().startswith('#'))
    text = text.replace('magicdatahub-combined', app).replace('127.0.0.1:25432', '127.0.0.1:0').replace('127.0.0.1:25433', '127.0.0.1:0')
    text = text.replace('../../var/combined/', 'state/').replace('allow_cross_database = false', 'allow_cross_database = true')
    text = text.replace('install_extensions = false', 'install_extensions = false\nextension_directory = ' + json.dumps(str(extensions)))
    # reader can connect to the explicit empty DB and raw source catalogs but has no data grants.
    text = text.replace('owner = "fedengine"', 'owner = "fedengine"\nconnect = ["reader"]')
    text += '\n[[users]]\nname="reader"\npassword_secret_ref="MDH_READER_PASSWORD"\n'
    for source, kind, catalog, schema, secret in [('crm', 'postgres', 'pg_crm', 'public', 'MDH_TEST_PG_DSN'), ('erp', 'mysql', 'mysql_erp', 'erp', 'MDH_TEST_MYSQL_DSN')]:
        text += f'\n[[datasources]]\nid="{source}"\nkind="{kind}"\ncatalog_alias="{catalog}"\nsecret_ref="{secret}"\ndefault_schema="{schema}"\nread_only=false\nddl_enabled=true\nallowed_schemas=["{schema}"]\n'
        text += f'\n[[grants]]\nprincipal="fedengine"\nsource="{source}"\nschema="*"\nobject="*"\nactions=["SELECT","INSERT","UPDATE","DELETE","CREATE_TABLE","DROP_TABLE","ALTER_TABLE"]\n'
        extension = LOCK['extensions'][kind]
        text += f'\n[[capabilities]]\nsource="{source}"\nduckdb_version="v2.0.0"\nextension_version="{extension["version"]}"\nremote_version="{extension["remote_version"]}"\nevidence="scripts/verify-federation.py owned sources"\noperations=["CREATE_TABLE","DROP_TABLE"]\nindex_adapter=false\n'
    (directory / 'fedengine.toml').write_text(text)
    return directory / 'host.toml'


def catalog(address, source, table):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        view = get(address, 'sources/' + source)
        if view['status'] == 'Active' and view.get('current_operation') is None:
            data = get(address, f'sources/{source}/catalog?object={table}&limit=10')
            if any(obj['name'] == table for obj in data['objects']):
                assert data['stale'] is False
                return
        time.sleep(0.05)
    raise AssertionError('DDL catalog publication timeout')


def management(address):
    def mutate(method, path, body, key):
        # PG Close/Cancel acknowledgements do not promise that the native lease
        # has exited. Only retry this explicit pre-publication rejection with
        # the identical idempotency key; never replay unknown writes here.
        deadline = time.monotonic() + 5
        while True:
            status, value = http(address, method, path, body, key)
            if status != 409 or value.get('code') != '55P03':
                return status, value
            assert time.monotonic() < deadline, 'native lease did not release before management publication'
            time.sleep(0.01)
    definition = {'name': 'api_owned', 'owner': 'fedengine', 'connect': [], 'allow_connections': True, 'schemas': []}
    status, created = mutate('POST', 'virtual-databases', definition, 'create-owned')
    assert status == 200, ('create virtual database', status, created.get('code'))
    status, replay = mutate('POST', 'virtual-databases', definition, 'create-owned')
    assert status == 200 and replay == created
    status, conflict = http(address, 'PUT', 'virtual-databases/api_owned', {
        'target': {'oid': created['oid'], 'expected_revision': created['revision'] + 1}, 'definition': definition}, 'stale-owned')
    assert status == 409 and conflict['code'] == '40001'
    definition['connect'] = ['reader']
    status, changed = mutate('PUT', 'virtual-databases/api_owned', {
        'target': {'oid': created['oid'], 'expected_revision': created['revision']}, 'definition': definition}, 'change-owned')
    assert status == 200 and changed['oid'] == created['oid'] and changed['revision'] > created['revision']
    return changed


def crud(listeners, sources, load):
    address, api = listeners['pgwire'], listeners['management']
    with Client(address, database='pg_crm') as pg, Client(address, database='mysql_erp') as my:
        for client, catalog_name, schema, source in [(pg, 'pg_crm', 'public', 'crm'), (my, 'mysql_erp', 'erp', 'erp')]:
            table = f'{catalog_name}.{schema}.mdh_items'
            client.query(f'CREATE TABLE {table}(id INTEGER, value INTEGER)')
            catalog(api, source, 'mdh_items')
            load.checkpoint()
            client.parse(f'INSERT INTO {table} VALUES($1::INTEGER,$2::INTEGER)', name='insert', types=(23, 23))
            client.bind([1, 10], statement='insert')
            assert client.execute()[1] == b'C'
            client.sync()
            client.query(f'UPDATE {table} SET value=20 WHERE id=1')
            assert client.query(f'SELECT value FROM {table} WHERE id=1') == [['20']]
            load.checkpoint()
            with Client(address, user='reader', database='fedengine') as denied:
                expect_state(lambda: denied.query(f'SELECT * FROM {table}'), '42501')
            expect_state(lambda: client.query(f'ALTER TABLE {table} ADD COLUMN forbidden INTEGER'), '0A000')
        assert pg.query('SELECT p.value+m.value FROM pg_crm.public.mdh_items p JOIN mysql_erp.erp.mdh_items m ON p.id=m.id') == [['40']]
        expect_state(lambda: pg.query('INSERT INTO pg_crm.public.mdh_items SELECT id,value FROM mysql_erp.erp.mdh_items'), '42501')
        with sources.pg() as native:
            assert native.query('SELECT id,value FROM public.mdh_items') == [['1', '20']]
        assert sources.mysql('SELECT id,value FROM erp.mdh_items') == '1\t20'
        for client, table in [(pg, 'pg_crm.public.mdh_items'), (my, 'mysql_erp.erp.mdh_items')]:
            client.query(f'DELETE FROM {table} WHERE id=1')
            assert client.query(f'SELECT count(*) FROM {table}') == [['0']]
        # Keep a small row for reopen and the blocked-write test.
        pg.query('INSERT INTO public.mdh_items VALUES(1,20)')
        my.query('DROP TABLE erp.mdh_items')
        load.checkpoint()
    print('Native PG/MySQL parameterized CRUD/JOIN, exact DDL gate, metadata and cross-source write rejection: Passed', flush=True)


def source_cas(address):
    before = get(address, 'sources/crm')
    body = {'target': target(before), 'secret_ref': 'MDH_TEST_PG_DSN'}
    status, receipt = http(address, 'PATCH', 'sources/crm', body, 'source-patch')
    assert status == 202
    finish(address, receipt)
    status, replay = http(address, 'PATCH', 'sources/crm', body, 'source-patch')
    assert status == 202 and replay['id'] == receipt['id']
    status, conflict = http(address, 'PATCH', 'sources/crm', body, 'source-stale')
    assert status == 409 and conflict['code'] == '40001'
    after = get(address, 'sources/crm')
    assert after['incarnation'] == before['incarnation'] and after['desired_revision'] > before['desired_revision']
    status, receipt = http(address, 'POST', 'sources/crm/apply', {'target': target(after)}, 'source-apply')
    assert status == 202
    finish(address, receipt)
    assert get(address, 'sources/crm')['status'] == 'Active'


def portal_drain(listeners, load):
    address, api = listeners['pgwire'], listeners['management']
    with Client(address, database='pg_crm') as pg:
        pg.parse('SELECT r.i::INTEGER FROM pg_crm.public.mdh_items t CROSS JOIN range(1000000) r(i)', name='source_portal')
        pg.bind(portal='source_portal', statement='source_portal')
        assert pg.execute(1, 'source_portal') == ([['0']], b's')
        source = get(api, 'sources/crm')
        status, receipt = http(api, 'POST', 'sources/crm/disable', {'target': target(source)}, 'disable-held-portal')
        assert status == 202
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            operation = get(api, 'operations/' + receipt['id'])
            if operation['status'] == 'WaitingForDrain':
                break
            assert operation['status'] not in ('Succeeded', 'Failed', 'Interrupted'), 'source lease released while Portal was still held'
            time.sleep(0.025)
        else:
            raise AssertionError('source did not wait for the retained Portal')
        load.checkpoint()
        pg.close_portal('source_portal')
        finish(api, receipt)
    source = get(api, 'sources/crm')
    assert source['status'] == 'Disabled'
    status, receipt = http(api, 'POST', 'sources/crm/apply', {'target': target(source)}, 'resume-held-portal')
    assert status == 202
    finish(api, receipt)
    assert get(api, 'sources/crm')['status'] == 'Active'
    load.checkpoint()
    print('Suspended remote Portal retains source lease; disable waits, Close releases and apply resumes: Passed', flush=True)


def blocked_write(listeners, sources, load):
    native = sources.pg()
    locked = False
    try:
        native.query("CREATE SEQUENCE public.mdh_attempts; CREATE FUNCTION public.mdh_block() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM nextval('public.mdh_attempts'); PERFORM set_config('lock_timeout','45s',true); PERFORM pg_advisory_xact_lock(11011011); RETURN NEW; END $$; CREATE TRIGGER mdh_gate BEFORE INSERT ON public.mdh_items FOR EACH ROW EXECUTE FUNCTION public.mdh_block()")
        native.query('SELECT pg_advisory_lock(11011011)')
        locked = True
        with Client(listeners['pgwire'], database='pg_crm') as pg:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                write = pool.submit(pg.query, 'INSERT INTO public.mdh_items VALUES(2,30)')
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if native.query("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event='advisory' AND query LIKE '%mdh_items%')") == [['t']]:
                        break
                    assert not write.done(), 'write ended before entering native lock'
                    time.sleep(0.025)
                else:
                    raise AssertionError('native write lock admission timeout')
                load.checkpoint()
                pg.cancel()
                expect_state(lambda: write.result(timeout=5), '57014')
                # Cancel receipt must not claim the native extension has released its source lease.
                assert native.query("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event='advisory' AND query LIKE '%mdh_items%')") == [['t']]
                load.checkpoint()
        return native
    except BaseException:
        if locked:
            native.query('SELECT pg_advisory_unlock(11011011)')
        native.close()
        raise


def slow_io_shutdown(process, listeners, sources, native):
    try:
        process.process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            incomplete = sum('StopIncomplete; retaining' in line for line in process.lines)
            if incomplete >= 2:
                break
            assert process.process.poll() is None, 'host exited while native worker was still blocked'
            time.sleep(0.01)
        else:
            raise AssertionError('host did not report repeated StopIncomplete')
        assert process.process.poll() is None
        assert native.query('SELECT last_value,is_called FROM public.mdh_attempts') == [['1', 't']]
    finally:
        # Release only the test-owned native lock, even on a failing assertion.
        native.query('SELECT pg_advisory_unlock(11011011)')
    try:
        process.stop(signal.SIGTERM)
        assert native.query('SELECT last_value,is_called FROM public.mdh_attempts') == [['1', 't']]
        assert native.query('SELECT count(*) FROM public.mdh_items WHERE id=2') in ([['0']], [['1']])
        native.query('DROP TRIGGER mdh_gate ON public.mdh_items; DROP FUNCTION public.mdh_block(); DROP SEQUENCE public.mdh_attempts')
        for address in listeners.values():
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(address)
    finally:
        native.close()
    print('Native blocked write: cancel retains worker, repeated StopIncomplete, unlock/drain, exactly one attempt and port release: Passed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, required=True)
    parser.add_argument('--extensions-dir', type=Path, required=True)
    parser.add_argument('--duckdb-library-dir', type=Path, required=True)
    args = parser.parse_args()
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    extensions = validate_extensions(args.extensions_dir)
    subprocess.run(['python3', str(smoke.ROOT / 'scripts/verify-dependency-identity.py')], cwd=smoke.ROOT, check=True)
    def sha(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    inputs = {}
    for repository in ['magicDataHub', 'modorch-mqtt', 'modorch-fedengine']:
        directory = smoke.ROOT if repository == 'magicDataHub' else smoke.ROOT.parent / repository
        def git(*arguments):
            return subprocess.check_output(['git', '-C', str(directory), *arguments], text=True).strip()
        inputs[repository] = {'head': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain', '--untracked-files=all'))}
    print(json.dumps({'platform': platform.platform(), 'git_inputs': inputs,
                      'cargo_lock_sha256': sha(smoke.ROOT / 'Cargo.lock'),
                      'source_identity_sha256': sha(smoke.ROOT / 'scripts/source-identity.json'),
                      'binaries_sha256': {name: sha(args.bin_dir.resolve() / name) for name in ['fedengine-server', 'datahub-server']},
                      'remote_reproducibility': 'NotRun'}), flush=True)
    env = dict(os.environ, FEDENGINE_PASSWORD=smoke.PASSWORD, MDH_READER_PASSWORD=smoke.PASSWORD, RUST_LOG='info')
    env['LD_LIBRARY_PATH'] = str(args.duckdb_library_dir.resolve()) + os.pathsep + env.get('LD_LIBRARY_PATH', '')
    with Sources() as sources:
        env.update(sources.environment())
        for combined in (False, True):
            name = 'datahub-server' if combined else 'fedengine-server'
            with tempfile.TemporaryDirectory(prefix='mdh-federation-') as temporary:
                directory = Path(temporary)
                config = configuration(directory, combined, extensions)
                checked = subprocess.run([str(args.bin_dir.resolve() / name), '--config', str(config), '--check-config'],
                                         cwd='/', env=env, capture_output=True, text=True, timeout=15)
                assert checked.returncode == 0, checked.stderr
                process = smoke.Process(args.bin_dir.resolve() / name, config, '/', env)
                blocked = None
                try:
                    listeners = process.ready()
                    with (MQTTLoad(listeners['mqtt']) if combined else NoMQTT()) as load:
                        protocol(listeners['pgwire'])
                        changed = management(listeners['management'])
                        crud(listeners, sources, load)
                        source_cas(listeners['management'])
                        portal_drain(listeners, load)
                        load.checkpoint()
                        blocked = blocked_write(listeners, sources, load)
                    slow_io_shutdown(process, listeners, sources, blocked)
                except BaseException:
                    diagnostics = ''.join(process.lines)[-6000:]
                    for value in [smoke.PASSWORD, SOURCE_PASSWORD, *sources.environment().values()]:
                        diagnostics = diagnostics.replace(value, '[redacted]')
                    print(diagnostics, flush=True)
                    raise
                finally:
                    try:
                        if blocked is not None and blocked.stream.fileno() != -1:
                            try:
                                blocked.query('SELECT pg_advisory_unlock(11011011)')
                            finally:
                                blocked.close()
                    finally:
                        process.close()
                # Reuse the same current DB and ensure API-created state was not replaced by TOML.
                process = smoke.Process(args.bin_dir.resolve() / name, config, '/', env)
                try:
                    reopened = process.ready()
                    state = get(reopened['management'], 'virtual-databases/api_owned')
                    assert state['oid'] == changed['oid'] and state['revision'] == changed['revision']
                    with Client(reopened['pgwire'], database='pg_crm') as pg:
                        assert pg.query('SELECT value FROM public.mdh_items WHERE id=1') == [['20']]
                        pg.query('DROP TABLE public.mdh_items')
                    process.stop(signal.SIGINT)
                finally:
                    process.close()
                print(name + ': management CAS/replay, current state reopen, signal drain: Passed', flush=True)
                print('test MDH-REMOTE-' + name + ' ... ok', flush=True)
        with tempfile.TemporaryDirectory(prefix='mdh-federation-client-') as temporary:
            directory = Path(temporary)
            config = configuration(directory, True, extensions)
            (directory / 'owned-fixture.marker').write_text('magicDataHub disposable sources\n')
            with sources.pg() as native:
                native.query('CREATE TABLE public.mdh_client_items(id INTEGER,value INTEGER); INSERT INTO public.mdh_client_items VALUES(1,20)')
            sources.mysql('CREATE TABLE erp.mdh_client_items(id INTEGER,value INTEGER); INSERT INTO erp.mdh_client_items VALUES(1,20)')
            test_env = dict(env, MDH_ACCEPTANCE_CONFIG=str(config))
            subprocess.run(['cargo', 'test', '-p', 'datahub-server', '--test', 'host', '--locked', '--offline',
                            'combined_remote_clients_enforce_object_grants_and_shared_admission', '--', '--ignored', '--exact'],
                           cwd=smoke.ROOT, env=test_env, check=True, timeout=180)
    print('Owned PG/MySQL containers and temporary host state removed', flush=True)


if __name__ == '__main__':
    main()
