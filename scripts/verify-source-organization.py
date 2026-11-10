#!/usr/bin/env python3
"""Verify deployed native-database grouping against an explicit local source plan."""
import argparse
import http.client
import json
import os
from pathlib import Path
from urllib.parse import quote

from acceptance.pg import Client


def native_key(kind, instance, database):
    endpoint = ('identity', instance['identity']) if instance.get('identity') else (
        'endpoint', instance['host'].rstrip('.').lower(), instance['port'])
    return kind, endpoint, database


def verify(sources, databases, plan):
    expected = {source['id']: source for source in plan['sources']}
    actual = {source['id']: source for source in sources}
    if len(expected) != len(plan['sources']) or set(expected) != set(actual):
        raise ValueError('source registrations do not match the explicit plan')
    groups, names, published = {}, {}, {}
    inactive = []
    for identity, record in expected.items():
        key = native_key(record['kind'], record, record['database'])
        name = record['virtual_database']
        if key in groups and groups[key] != name:
            raise ValueError('same native database was split into multiple virtual databases')
        if name in names and names[name] != key:
            raise ValueError('different native databases were collapsed into one virtual database')
        groups[key], names[name] = name, key
        source = actual[identity]
        definition = source['desired']['definition']
        target = definition.get('organization')
        if not target:
            raise ValueError('source lacks native-database organization: ' + identity)
        if (native_key(source['kind'], target['instance'], target['database']) != key
                or target['virtual_database'] != name):
            raise ValueError('source target differs from its planned native database: ' + identity)
        if definition['allowed_schemas'] != [record['schema']]:
            raise ValueError('source schema differs from its explicit scope: ' + identity)
        if source.get('catalog_snapshot'):
            published.setdefault(name, set()).add((record['schema'], identity, record['schema']))
        if source['status'] != 'Active':
            inactive.append(identity)
    by_name = {database['config']['name']: database for database in databases}
    if set(by_name) != set(published) | set(plan.get('account_databases', [])):
        raise ValueError('unexpected, missing, or aggregate virtual database')
    for name, mappings in published.items():
        database = by_name[name]
        actual_mappings = [(mapping['name'], mapping['source'], mapping['remote_schema'])
                           for mapping in database['config']['schemas']]
        if not database['managed'] or set(actual_mappings) != mappings or len(actual_mappings) != len(mappings):
            raise ValueError('automatic database has renamed or incorrect source contributions: ' + name)
    return {'native_databases': len(groups), 'published_databases': sorted(published),
            'inactive_sources': inactive, 'planned_sources': len(expected)}


def get(address, endpoint):
    connection = http.client.HTTPConnection(*address, timeout=30)
    try:
        connection.request('GET', '/api/v1/' + endpoint)
        response = connection.getresponse()
        body = response.read(4 * 1024 * 1024 + 1)
        if response.status != 200 or len(body) > 4 * 1024 * 1024:
            raise ValueError('management read failed')
        return json.loads(body)
    finally:
        connection.close()


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True, help='local plan with endpoints and SecretRefs, never passwords')
    parser.add_argument('--management', type=int, required=True, help='loopback management port')
    parser.add_argument('--pgwire', type=int, required=True, help='loopback PGWire port')
    parser.add_argument('--password-env', default='FEDENGINE_PASSWORD')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    address = ('127.0.0.1', args.management)
    sources = get(address, 'sources')
    databases = get(address, 'virtual-databases')
    result = verify(sources, databases, plan)
    password = os.environ[args.password_env]
    with Client(('127.0.0.1', args.pgwire), password=password) as client:
        visible = {row[0] for row in client.query('SELECT datname FROM pg_catalog.pg_database')}
        if visible != {database['config']['name'] for database in databases}:
            raise ValueError('PGWire database visibility differs from management organization')
    samples = 0
    by_id = {source['id']: source for source in sources}
    for record in plan['sources']:
        if by_id[record['id']]['status'] != 'Active':
            continue
        objects = get(address, 'sources/' + quote(record['id'], safe='') + '/catalog?limit=100')['objects']
        sample = next(((obj, column) for obj in objects if obj['schema'] == record['schema']
                       for column in obj['columns'] if column['supported'] and column['mapped_type']), None)
        if sample is None:
            raise ValueError('active source has no sampleable object in first catalog page: ' + record['id'])
        obj, column = sample
        sql = (
            'SELECT CAST(' + identifier(column['name']) + ' AS TEXT) FROM '
            + identifier(record['schema']) + '.' + identifier(obj['name']) + ' LIMIT 1')
        with Client(('127.0.0.1', args.pgwire), database=record['virtual_database'], password=password) as client:
            client.query(sql)
        samples += 1
    result.update(status='Passed', scope='organization-and-active-source-reads', sample_queries=samples,
                  all_sources_active=not result['inactive_sources'])
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
