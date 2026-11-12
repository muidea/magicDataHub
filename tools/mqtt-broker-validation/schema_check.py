"""Offline validator for the explicit JSON Schema subset used by this tool.

Not a general JSON Schema implementation. Unsupported keywords fail closed;
external consumers may use a full Draft 2020-12 validator on the same schemas.
"""
import json
import math
import re
from functools import lru_cache

from common import InputError, ROOT, read_json

KEYWORDS = {'$schema', 'title', 'description', 'type', 'const', 'enum', 'properties',
            'required', 'additionalProperties', 'items', 'minItems', 'uniqueItems',
            'minLength', 'pattern', 'minimum', 'maximum', 'maxItems', 'oneOf', 'prefixItems'}
TYPES = {'object': lambda x: isinstance(x, dict), 'array': lambda x: isinstance(x, list),
         'string': lambda x: isinstance(x, str), 'boolean': lambda x: type(x) is bool,
         'integer': lambda x: type(x) is int,
         'number': lambda x: type(x) in (int, float) and math.isfinite(x),
         'null': lambda x: x is None}


def schema_definition(schema):
    if not isinstance(schema, dict) or set(schema) - KEYWORDS:
        raise InputError('unsupported schema keyword or schema shape')
    kinds = schema.get('type', [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not isinstance(kinds, list) or not all(k in TYPES for k in kinds):
        raise InputError('unsupported schema type')
    if 'type' in schema and (not kinds or len(set(kinds)) != len(kinds)):
        raise InputError('empty or duplicate schema types')
    for key in ['minItems', 'maxItems', 'minLength']:
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise InputError('invalid nonnegative schema bound')
    for key in ['minimum', 'maximum']:
        if key in schema and not TYPES['number'](schema[key]):
            raise InputError('invalid numeric schema bound')
    if 'uniqueItems' in schema and type(schema['uniqueItems']) is not bool:
        raise InputError('invalid schema uniqueness flag')
    for key in ['title', 'description', '$schema', 'pattern']:
        if key in schema and not isinstance(schema[key], str):
            raise InputError('invalid schema text')
    if 'pattern' in schema:
        try:
            re.compile(schema['pattern'])
        except re.error as error:
            raise InputError('invalid schema pattern') from error
    if 'required' in schema and (not isinstance(schema['required'], list)
            or not all(isinstance(x, str) for x in schema['required'])
            or len(set(schema['required'])) != len(schema['required'])):
        raise InputError('invalid required schema fields')
    if 'properties' in schema and not isinstance(schema['properties'], dict):
        raise InputError('invalid schema properties')
    if 'items' in schema and not isinstance(schema['items'], dict):
        raise InputError('unsupported item schema')
    if 'additionalProperties' in schema and not isinstance(schema['additionalProperties'], (dict, bool)):
        raise InputError('invalid additional property schema')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise InputError('invalid schema enumeration')
    if 'prefixItems' in schema and (not isinstance(schema['prefixItems'], list) or not schema['prefixItems']):
        raise InputError('invalid tuple schema')
    if 'oneOf' in schema:
        if not isinstance(schema['oneOf'], list) or not schema['oneOf']:
            raise InputError('invalid oneOf schema')
        for child in schema['oneOf']:
            schema_definition(child)
    for child in schema.get('prefixItems', []):
        schema_definition(child)
    for child in schema.get('properties', {}).values():
        schema_definition(child)
    for key in ['items', 'additionalProperties']:
        if isinstance(schema.get(key), dict):
            schema_definition(schema[key])


def validate(value, schema, path='$'):
    if 'oneOf' in schema:
        matched = 0
        for child in schema['oneOf']:
            try:
                validate(value, child, path)
                matched += 1
            except InputError:
                pass
        if matched != 1:
            raise InputError('schema requires exactly one matching variant at ' + path)
    kinds = schema.get('type')
    if kinds is not None and not any(TYPES[k](value) for k in ([kinds] if isinstance(kinds, str) else kinds)):
        raise InputError('schema type mismatch at ' + path)
    # JSON booleans and numbers are distinct even though Python treats True == 1.
    canonical = lambda x: json.dumps(x, sort_keys=True, allow_nan=False)
    if 'const' in schema and canonical(value) != canonical(schema['const']):
        raise InputError('schema constant mismatch at ' + path)
    if 'enum' in schema and canonical(value) not in [canonical(x) for x in schema['enum']]:
        raise InputError('schema enum mismatch at ' + path)
    if isinstance(value, dict):
        if not set(schema.get('required', [])) <= set(value):
            raise InputError('schema required field missing at ' + path)
        properties = schema.get('properties', {})
        for key, item in value.items():
            child = properties.get(key, schema.get('additionalProperties', {}))
            if child is False:
                raise InputError('schema unknown field at ' + path + '.' + key)
            if isinstance(child, dict):
                validate(item, child, path + '.' + key)
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', len(value)):
            raise InputError('schema array too short at ' + path)
        if schema.get('uniqueItems') and len({canonical(x) for x in value}) != len(value):
            raise InputError('schema duplicate array item at ' + path)
        if 'items' in schema:
            for index, item in enumerate(value):
                if index < len(schema.get('prefixItems', [])):
                    continue
                validate(item, schema['items'], path + '[' + str(index) + ']')
        for index, child in enumerate(schema.get('prefixItems', [])):
            if index < len(value):
                validate(value[index], child, path + '[' + str(index) + ']')
    if isinstance(value, str) and (len(value) < schema.get('minLength', 0) or
            'pattern' in schema and re.search(schema['pattern'], value) is None):
        raise InputError('schema string mismatch at ' + path)
    if type(value) in (int, float) and 'minimum' in schema and value < schema['minimum']:
        raise InputError('schema value below minimum at ' + path)
    if type(value) in (int, float) and 'maximum' in schema and value > schema['maximum']:
        raise InputError('schema value above maximum at ' + path)


@lru_cache(maxsize=16)
def load_schema(name):
    schema = read_json(ROOT / 'schemas' / (name + '.schema.json'))
    schema_definition(schema)
    return schema


def check(name, value):
    validate(value, load_schema(name))
