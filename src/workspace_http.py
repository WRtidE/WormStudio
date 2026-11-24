"""Run socket I/O and a single prediction alongside serialized workspace commands."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from functools import wraps
from http.server import ThreadingHTTPServer
import secrets
import socket
import threading
from typing import NamedTuple


class Prediction(NamedTuple):
    key: str
    token: str
    future: Future


def workspace_request(method):
    @wraps(method)
    def serialized(handler):
        with handler.server.control:
            if not handler.server.closing:
                return method(handler)
    return serialized


class WorkspaceHTTPServer(ThreadingHTTPServer):
    daemon_threads = False  # Finish cleanup in request handlers before disposing of models.

    def __init__(self, address, handler):
        self.control = threading.Lock()
        self.closing = False
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.predictions = ThreadPoolExecutor(max_workers=1, thread_name_prefix='annotation-predict')
        self.prediction = None
        super().__init__(address, handler)

    @contextmanager
    def outside_control(self):
        """Called inside a workspace request, for work on private files or a future."""
        self.control.release()
        try:
            yield
        finally:
            self.control.acquire()
        if self.closing:
            raise ConnectionAbortedError('工作台正在关闭')

    def submit_prediction(self, annotation, body):
        if self.prediction and not self.prediction.future.done():
            return None
        key = secrets.token_urlsafe(18)
        future = self.predictions.submit(annotation.predict, body, token=annotation.token)
        self.prediction = Prediction(key, annotation.token, future)
        return self.prediction

    def get_request(self):
        connection, address = super().get_request()
        with self.connections_lock:
            self.connections.add(connection)
        return connection, address

    def close_request(self, request):
        with self.connections_lock:
            self.connections.discard(request)
        super().close_request(request)

    def server_close(self):
        # Stop idle browser connections and interrupted transfers before joining
        # their threads. Let the command that triggered shutdown finish replying.
        with self.control:
            self.closing = True
            with self.connections_lock:
                for connection in self.connections:
                    with suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)
        super().server_close()
        self.predictions.shutdown(wait=True, cancel_futures=True)
        self.prediction = None
