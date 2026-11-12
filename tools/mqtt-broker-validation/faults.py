"""One-connection, frame-preserving TCP proxy with an explicit ACK cut boundary."""
import socket
import threading

from wire import read_frame


class CutProxy:
    def __init__(self, endpoint, trace, ack):
        self.endpoint, self.trace, self.ack = endpoint, trace, ack
        self.stopped, self.cut = threading.Event(), threading.Event()
        self.streams, self.pumps, self.errors = [], [], []
        self.dropped = None

    def __enter__(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.address = self.listener.getsockname()
        self.thread = threading.Thread(target=self._accept)
        self.thread.start()
        return self

    def _close_streams(self):
        self.stopped.set()
        for stream in self.streams[:]:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()

    def _accept(self):
        try:
            client, _ = self.listener.accept()
            self.streams.append(client)
            broker = socket.create_connection(self.endpoint, timeout=5)
            self.streams.append(broker)
            if self.stopped.is_set():
                self._close_streams()
                return
            for stream in self.streams:
                stream.settimeout(5)
            self.trace.event('proxy-target', peer=list(broker.getpeername()), listener=list(self.address))
            for source, destination, direction in [(client, broker, 'client-to-broker'), (broker, client, 'broker-to-client')]:
                pump = threading.Thread(target=self._pump, args=(source, destination, direction))
                self.pumps.append(pump)
                pump.start()
            for pump in self.pumps:
                pump.join()
        except Exception as error:
            if not self.stopped.is_set():
                self.errors.append(type(error).__name__)
            self._close_streams()
        finally:
            self._close_streams()

    def _pump(self, source, destination, direction):
        try:
            while not self.stopped.is_set():
                chunks = []
                def capture(part):
                    chunks.append(part)
                    self.trace.event('proxy-bytes', direction=direction, operation='receive', hex=part.hex())
                frame = read_frame(source, capture)
                raw = b''.join(chunks)
                # Forward the exact bytes read, including the original length encoding.
                if direction == 'broker-to-client' and frame[0] == self.ack:
                    self.dropped = frame
                    self.trace.event('fault-cut', ack_kind=frame[0], ack_body_hex=frame[1].hex(),
                                     hex=raw.hex(), forwarded=False, boundary='proxy received ACK; client did not')
                    self.cut.set()
                    self._close_streams()
                    return
                destination.sendall(raw)
                self.trace.event('proxy-bytes', direction=direction, operation='forward', hex=raw.hex())
        except Exception as error:
            if not self.stopped.is_set():
                self.errors.append(type(error).__name__)
            self._close_streams()

    def __exit__(self, exc_type, *_):
        self._close_streams()
        self.listener.close()
        self.thread.join(timeout=6)
        alive = self.thread.is_alive() or any(p.is_alive() for p in self.pumps)
        self.trace.event('proxy-cleanup', complete=not alive and not self.errors, errors=self.errors)
        if exc_type is None:
            self.trace.check(not alive and not self.errors, 'qos2-proxy-cleanup')
