"""Reviewed B-class upstream environment adapter; original assertions preserved."""
import ast
import sys
import uuid

from common import digest


class CapturedSocket:
    def __init__(self, stream, trace):
        self.stream, self.trace = stream, trace
        self.identity = uuid.uuid4().hex[:12]
        trace.event('connection', connection=self.identity, peer=list(stream.getpeername()))

    def send(self, body, *args):
        count = self.stream.send(body, *args)
        self.trace.event('bytes', connection=self.identity, direction='tx', hex=body[:count].hex())
        return count

    def sendall(self, body, *args):
        self.stream.sendall(body, *args)
        self.trace.event('bytes', connection=self.identity, direction='tx', hex=body.hex())

    def recv(self, count, *args):
        body = self.stream.recv(count, *args)
        self.trace.event('bytes', connection=self.identity, direction='rx', hex=body.hex())
        return body

    def __getattr__(self, name):
        return getattr(self.stream, name)


def upstream_case(upstream, filename, endpoint, trace, parameters):
    """B adapter: original import/functions/ACK assertions; no MQTT5 invocation."""
    sys.path.insert(0, str(upstream / 'test/broker'))
    sys.path.insert(0, str(upstream / 'test'))
    import mosq_test
    original_connect, original_matches = mosq_test.client_connect_only, mosq_test.packet_matches
    original_unordered = mosq_test.receive_unordered
    old = {name: getattr(mosq_test, name) for name in ['get_port', 'start_broker', 'client_connect_only', 'packet_matches', 'receive_unordered']}

    class ExternalLifecycle:
        def terminate(self):
            trace.event('upstream-lifecycle', operation='terminate-requested')

        def wait(self):
            return 0

        def communicate(self):
            return b'', b''

    def start_broker(*args, **kwargs):
        if kwargs.get('use_conf') or kwargs.get('cmd') or kwargs.get('expect_fail'):
            raise RuntimeError('unreviewed upstream environment request')
        trace.check(kwargs.get('port') == endpoint[1], 'upstream-target-port-bound')
        return ExternalLifecycle()

    def client_connect_only(hostname='localhost', port=1888, timeout=10):
        trace.check(hostname in ['localhost', '127.0.0.1'] and port == endpoint[1], 'upstream-target-address-bound')
        return CapturedSocket(original_connect('127.0.0.1', port, min(timeout, 5)), trace)

    def packet_matches(name, received, expected):
        result = original_matches(name, received, expected)
        trace.event('upstream-assertion', assertion=name, passed=result,
                    observed=received.hex(), expected=expected.hex())
        return result

    def receive_unordered(stream, first, second, name):
        try:
            result = original_unordered(stream, first, second, name)
        except Exception:
            trace.event('upstream-assertion', assertion=name, passed=False)
            raise
        trace.event('upstream-assertion', assertion=name, passed=True,
                    expected_variants=[(first + second).hex(), (second + first).hex()])
        return result

    try:
        mosq_test.get_port = lambda: endpoint[1]
        mosq_test.start_broker = start_broker
        mosq_test.client_connect_only = client_connect_only
        mosq_test.packet_matches = packet_matches
        mosq_test.receive_unordered = receive_unordered
        path = upstream / 'test/broker' / filename
        tree = ast.parse(path.read_text(), filename=str(path))
        allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef)
        if not all(isinstance(node, allowed + (ast.Expr,)) for node in tree.body):
            raise RuntimeError('unreviewed upstream module structure')
        module = ast.Module(body=[node for node in tree.body if isinstance(node, allowed)], type_ignores=[])
        namespace = {'__file__': str(path), '__name__': '__upstream_validation__'}
        exec(compile(module, str(path), 'exec'), namespace)
        trace.event('upstream-selection', file=filename, sha256=digest(path), reuse='B', parameters=parameters,
                    limitations=['fixed Packet ID/ordering assertions may be broker-specific',
                                 'upstream negative observations are bounded/limited',
                                 'lifecycle delegated to controlled outer adapter'])
        namespace['do_test'](**parameters)
    finally:
        for name, value in old.items():
            setattr(mosq_test, name, value)


