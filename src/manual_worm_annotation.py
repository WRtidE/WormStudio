"""Compatibility entry point for workspace servers and interactive annotation."""
from annotation_session import (Annotation as Session, advance_sequence, can_edit_annotation_mode,
                                change_annotation_mode, save_chambers)
from image_io import png
from platforms.system import is_darwin
from resource_paths import PROJECT_ROOT as ROOT
from sam2_mask_predictor import MaskPredictor, annotation_worker
from server_routes import create_server as workspace_server

__all__ = [
    'Annotation', 'MaskPredictor', 'ROOT', 'Session', 'advance_sequence', 'annotation_worker',
    'can_edit_annotation_mode', 'change_annotation_mode', 'collect_annotation', 'create_server',
    'is_darwin', 'new_video_annotation', 'png', 'save_chambers', 'workspace_server',
]


class Annotation(Session):
    def __init__(self, settings, frame=0, *, root=None, preview_seconds=None):
        super().__init__(settings, frame, root=ROOT if root is None else root, preview_seconds=preview_seconds)


def new_video_annotation(video, defaults):
    return Annotation.from_video(video, root=ROOT)


def create_server(annotation=None, progress=None):
    return workspace_server(annotation, progress, root=ROOT, annotation_type=Annotation)


def collect_annotation(settings, frame=0, reuse=True, check_only=False, open_browser=True, review_saved=True, progress=None):
    # A queued editor may already have been saved or cancelled while the worker
    # was loading dependencies. Its event and result belong to this dispatch.
    handed_off = progress.worker_annotation if progress else None
    existing = getattr(progress.server, 'annotation', None) if progress and progress.server else None
    if handed_off is not None:
        annotation = handed_off
    elif existing and not existing.done.is_set() and existing.settings == settings:
        annotation = existing
    else:
        annotation = Annotation(settings, frame)
    if reuse:
        previous = annotation.previous()
        if previous is not None and (check_only or not review_saved):
            print(f'使用已保存的人工标注：{annotation.path}', flush=True)
            return previous
    if check_only:
        print('视频和标注帧可读取。运行时将打开人工标注页面；当前尚未保存标注。', flush=True)
        return None
    reused_server = progress is not None and progress.server is not None
    server = progress.server if reused_server else create_server(annotation, progress)
    if handed_off is None:
        server.annotation = annotation
    server.timeout = .5
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'请在工作台完成标注并点击“保存并开始处理”：{url}', flush=True)
    if progress and not reused_server: progress.bind_server(server)
    close_window = None
    if open_browser and not reused_server:
        from electron_desktop import open_view
        def window_closed():
            if progress:
                progress.cancel_requested.set()
                progress.closed.set()
            current = getattr(server, 'annotation', None)
            if current:
                current.done.set()
            annotation.done.set()
        close_window = open_view(url, window_closed)
    try:
        while not annotation.done.is_set():
            if progress: annotation.done.wait(.2)
            else: server.handle_request()
    finally:
        if not progress:
            if close_window: close_window()
            server.server_close()
        annotation.release_predictor()
    return annotation.result
