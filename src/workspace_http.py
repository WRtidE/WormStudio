"""Run socket I/O and a single prediction alongside serialized workspace commands."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from functools import wraps
from http.server import ThreadingHTTPServer
from pathlib import Path
import cv2
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
            if handler.server.closing:
                return
            hosts = handler.headers.get_all('Host', [])
            if len(hosts) != 1 or hosts[0].lower() not in handler.server.allowed_hosts:
                with handler.server.outside_control():
                    handler.respond_before_body(403, dict(error='无效的工作台地址'))
                return
            return method(handler)
    return serialized


class PreviewFrameReader:
    """Own one decoder; positioning and reading always happen under the same lock."""
    def __init__(self):
        self.lock = threading.Lock()
        self.capture = None
        self.source = None
        self.next_frame = None

    def read(self, video, frame):
        with self.lock:
            stat = Path(video).stat()
            source = (video, stat.st_size, stat.st_mtime_ns)
            if source != self.source:
                self.close()
                self.capture = cv2.VideoCapture(str(video))
                if not self.capture.isOpened():
                    self.close()
                    raise ValueError('无法打开预览视频')
                self.source = source
                self.next_frame = 0
            if not 0 <= frame < int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT)):
                raise ValueError('帧号超出范围')
            if frame != self.next_frame and not self.capture.set(cv2.CAP_PROP_POS_FRAMES, frame):
                self.close()
                raise ValueError('无法定位指定帧')
            ok, image = self.capture.read()
            if not ok:
                self.close()
                raise ValueError('无法解码此帧')
            self.next_frame = frame + 1
            return image

    def close(self):
        if self.capture is not None:
            self.capture.release()
        self.capture = self.source = self.next_frame = None


class WorkspaceHTTPServer(ThreadingHTTPServer):
    daemon_threads = False  # Finish cleanup in request handlers before disposing of models.

    def __init__(self, address, handler):
        self.control = threading.Lock()
        self.closing = False
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.predictions = ThreadPoolExecutor(max_workers=1, thread_name_prefix='annotation-predict')
        self.prediction = None
        self.preview_reader = PreviewFrameReader()
        super().__init__(address, handler)
        self.allowed_hosts = {f'127.0.0.1:{self.server_port}', f'localhost:{self.server_port}'}

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
        self.preview_reader.close()
        self.predictions.shutdown(wait=True, cancel_futures=True)
        self.prediction = None
