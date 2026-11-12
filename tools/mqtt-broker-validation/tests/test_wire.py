"""Guard against false silence and invalid ordering assumptions in the test tool."""
import collections
from pathlib import Path
import socket
import tempfile
import unittest

from scenarios import calibration
from wire import Client, Trace, packet, string


class WireValidation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mqtt-wire-check-')
        self.addCleanup(self.directory.cleanup)
        self.trace = Trace(Path(self.directory.name) / 'trace.jsonl')
        self.stream, self.peer = socket.socketpair()
        self.stream.settimeout(0.05)
        self.addCleanup(self.stream.close)
        self.addCleanup(self.peer.close)
        self.client = Client.__new__(Client)
        self.client.stream = self.stream
        self.client.trace = self.trace
        self.client.identity = 'unit'
        self.client.pending = collections.deque()

    def test_publish_before_suback_is_buffered_and_valid(self):
        self.peer.sendall(packet(0x30, string('topic') + b'value') + packet(0x90, b'\x00\x09\x01'))
        self.client.expect(0x90, b'\x00\x09\x01')
        self.client.delivery('topic', b'value', qos=0, retain=False)

    def test_partial_packet_timeout_cannot_pass_absence_assertion(self):
        self.peer.sendall(b'\x30')
        with self.assertRaises(socket.timeout):
            self.client.no_message(0.01)

    def test_peer_eof_cannot_pass_absence_assertion(self):
        self.peer.close()
        with self.assertRaises(EOFError):
            self.client.no_message(0.01)

    def test_pending_publish_cannot_pass_absence_assertion(self):
        self.client.pending.append((0x30, string('topic') + b'value'))
        with self.assertRaisesRegex(AssertionError, 'no-pending'):
            self.client.no_message(0.01)

    def test_wrong_ack_id_is_detected(self):
        self.peer.sendall(packet(0x50, b'\x00\x18'))
        with self.assertRaisesRegex(AssertionError, 'expected-control'):
            self.client.expect(0x50, b'\x00\x17')

    def test_error_peers_are_detected_over_real_loopback(self):
        calibration(self.trace)


if __name__ == '__main__':
    unittest.main()
