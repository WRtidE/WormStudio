"""HTTP routes for annotation, result review, and the local workspace."""
import copy
from functools import partial
import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import cv2

from annotation_session import (Annotation, advance_sequence, can_edit_annotation_mode,
                                change_annotation_mode, save_chambers)
from image_io import png
from input_validation import reserved_filename
from resource_paths import PROJECT_ROOT
from workspace_http import WorkspaceHTTPServer, workspace_request


def create_server(annotation=None, progress=None, *, root=PROJECT_ROOT, annotation_type=Annotation):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def respond(self, code, data, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes): data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(data)

        def respond_before_body(self, code, data):
            """Send the response first, then briefly drain a body already in flight."""
            self.close_connection = True
            self.respond(code, data)
            try:
                remaining = int(self.headers.get('Content-Length', '0'))
                if remaining <= 0:
                    return
                # Closing with unread TCP data can discard the response on Windows.
                self.connection.shutdown(socket.SHUT_WR)
                deadline = time.monotonic() + 0.5
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, ValueError):
                # A disconnected peer or invalid length needs no further reads.
                return

        def send_static(self, name):
            types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png'}
            with self.server.outside_control():
                path = root / 'web' / name
                self.respond(200, path.read_bytes(), types[path.suffix] + ('; charset=utf-8' if path.suffix != '.png' else ''))

        def send_artifact(self, library=False):
            from urllib.parse import quote
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if library:
                from workspace_library import media
                path = media(root, query.get('id', [''])[0])
            elif 'chamber_video' in query:
                try:
                    path = self.server.chamber_videos.artifact(query['chamber_video'][0])
                except ValueError:
                    self.respond(404, {}); return
            else:
                key = query.get('key', [''])[0]
                sample = query.get('run', [None])[0]
                if query.get('history') == ['1']:
                    from run_storage import result_artifacts
                    try:
                        with self.server.outside_control():
                            archives = result_artifacts(*self.read_archive(sample))
                            path = archives[key][1]
                    except (KeyError, OSError, TypeError, ValueError):
                        self.respond(404, {}); return
                elif 'group' in query:
                    from workspace_sessions import load_config
                    from run_storage import saved_videos
                    try:
                        with progress.lock:
                            items = progress.batch.group_items(query['group'][0] or None) if progress.batch else []
                        if not any(i['sample'] == sample and i['status'] == 'complete' for i in items):
                            self.respond(404, {}); return
                        path = saved_videos(*load_config(root, sample))[key][1]
                    except (KeyError, OSError, TypeError, ValueError):
                        self.respond(404, {}); return
                else:
                    with progress.lock:
                        if sample is not None and sample != progress.data['sample']:
                            self.respond(409, dict(error='结果会话已改变')); return
                        path = progress.artifacts.get(key)
            if path is None or not path.is_file(): self.respond(404, {}); return
            total = path.stat().st_size
            start, end, code = 0, total-1, 200
            requested = self.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                if match and any(match.groups()):
                    left, right = match.groups()
                    start = int(left) if left else max(0, total-int(right))
                    end = min(int(right), total-1) if left and right else total-1
                    code = 206
                if not match or not any(match.groups()) or start > end or start >= total:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{total}')
                    self.send_header('Content-Length', '0'); self.end_headers(); return
            inline = query.get('inline') == ['1']
            self.send_response(code)
            from workspace_library import content_type
            self.send_header('Content-Type', content_type(path) if library else 'video/mp4' if inline and path.suffix.lower()=='.mp4' else 'application/octet-stream')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(end-start+1))
            self.send_header('Accept-Ranges', 'bytes')
            if code == 206: self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
            self.send_header('Content-Disposition', ('inline' if inline else 'attachment') + "; filename*=UTF-8''" + quote(path.name))
            self.end_headers()
            if self.command == 'HEAD': return
            try:
                with path.open('rb') as handle, self.server.outside_control():
                    handle.seek(start)
                    remaining = end-start+1
                    while remaining:
                        chunk = handle.read(min(1024*1024, remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

        @workspace_request
        def do_HEAD(self):
            if progress and self.path.startswith('/api/library-media?'): self.send_artifact(library=True)
            elif progress and self.path.startswith('/artifact?'): self.send_artifact()
            else: self.send_response(404); self.end_headers()

        def get_head_preview(self):
            annotation = self.server.annotation
            if not annotation or self.headers.get('X-Annotation-Token') != annotation.token:
                self.respond(403, dict(error='标注会话已改变')); return
            try:
                frame = int(parse_qs(urlsplit(self.path).query).get('frame', [''])[0])
                if not 0 <= frame < annotation.n: raise ValueError('帧号超出范围')
                token = annotation.token
                x, y, x1, y1 = annotation.crop
                with self.server.outside_control():
                    image = self.server.preview_reader.read(annotation.video, frame)
                    data = png(image[y:y1, x:x1])
                if self.server.annotation is not annotation or token != annotation.token:
                    self.respond(409, dict(error='标注会话已改变')); return
                with self.server.outside_control():
                    self.respond(200, data, 'image/png')
            except (ValueError, OSError, cv2.error) as error:
                self.respond(400, dict(error=str(error)))
            return

        def get_prediction_status(self):
            annotation = self.server.annotation
            if not annotation or self.headers.get('X-Annotation-Token') != annotation.token:
                self.respond(403, dict(error='标注会话已改变')); return
            job = self.server.prediction
            key = parse_qs(urlsplit(self.path).query).get('job', [None])[0]
            if not job or job.key != key or job.token != annotation.token or annotation.done.is_set():
                self.respond(409, dict(error='本次预测已失效，请重新点选')); return
            if not job.future.done():
                self.respond(202, dict(job=key)); return
            try:
                result = job.future.result()
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, dict(error=str(error))); return
            except Exception as error:
                self.respond(500, dict(error=f'SAM2 预测失败：{error}')); return
            self.respond(200, result); return

        def get_channel_view(self):
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            file = query.get('file', [''])[0]
            run = query.get('run', [None])[0]
            try:
                counts = self.server.channel_counts()
                with self.server.outside_control():
                    if route.path == '/api/channel/batch':
                        self.respond(200, counts.batch.snapshot())
                    elif route.path == '/api/channel/state':
                        view = counts.state(file, run)
                        if view['task'] and view['task']['status'] != 'complete':
                            view['task'].pop('events', None)
                        self.respond(200, view)
                    elif route.path == '/api/channel/batch-export':
                        self.respond(200, counts.export_batch(query.get('id', [None])[0]), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    elif route.path == '/api/channel/comparison':
                        self.respond(200, counts.comparison(include_removed=query.get('include_removed') == ['1']))
                    elif route.path == '/api/channel/setup':
                        self.respond(200, counts.setup(file, int(query.get('channels', [32])[0]), int(query.get('frame', [0])[0]), run))
                    elif route.path == '/api/channel/frames':
                        self.respond(200, counts.frames(file, run, int(query.get('start', ['1'])[0]), int(query.get('limit', ['6'])[0])))
                    elif route.path == '/api/channel/frame-image':
                        number = int(query.get('frame', ['0'])[0])
                        self.respond(200, counts.frame_image(file, run, number, self.server.preview_reader), 'image/jpeg')
                    elif route.path == '/api/channel/event':
                        self.respond(200, counts.event(file, run, int(query.get('id', [0])[0])))
                    elif route.path == '/api/channel/export':
                        channel = int(query['channel'][0]) if 'channel' in query else None
                        self.respond(200, counts.export(file, run, channel), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    else: self.respond(404, {})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except (ValueError, OSError, RuntimeError, cv2.error) as exc:
                self.respond(400, dict(error=str(exc)))
            return

        def get_layout(self):
            annotation = self.server.annotation
            from workspace_layout import read
            token = progress.token if progress else annotation.token if annotation else None
            if not token: self.respond(404, {}); return
            try: self.respond(200, dict(read(root), token=token))
            except (ValueError, OSError) as exc: self.respond(500, dict(error=str(exc), token=token))

        def get_library(self):
            from workspace_library import listing
            try: self.respond(200, listing(root))
            except (ValueError, OSError) as exc: self.respond(500, dict(error='文件工作区读取失败：'+str(exc)))

        def get_library_media(self):
            self.send_artifact(library=True)

        def get_evaluation(self):
            from evaluation_http import get
            get(self)

        def get_projects(self):
            from workspace_sessions import catalog
            self.respond(200,catalog(root))

        def get_identity(self):
            from workspace_sessions import identity_links, identity_frame
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            sample = query.get('run', [''])[0]
            with progress.lock:
                config = progress.config
            if not config or config['sample'] != sample:
                self.respond(409, dict(error='结果会话已变化')); return
            try:
                frame = route.path == '/api/identity-frame'
                with self.server.outside_control():
                    if frame:
                        data = identity_frame(root, sample, int(query.get('worm', ['0'])[0]), int(query.get('frame', ['-1'])[0]))
                    else:
                        data = identity_links(root, sample)
                with progress.lock:
                    changed = progress.config is not config
                if changed:
                    self.respond(409, dict(error='结果会话已变化')); return
                with self.server.outside_control():
                    self.respond(200, data, 'image/jpeg' if frame else 'application/json; charset=utf-8')
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.respond(400, dict(error=str(exc)))

        def get_head_anchors(self):
            from head_evidence import normalize_anchors
            sample=parse_qs(urlsplit(self.path).query).get('run',[''])[0]
            with progress.lock:
                config = progress.config
            if not config or config['sample']!=sample:
                self.respond(409,dict(error='结果会话已变化'));return
            self.respond(200,dict(sample=sample,anchors={w:normalize_anchors(a) for w,a in config.get('head_annotations',{}).items()}))

        def get_draft(self):
            annotation = self.server.annotation
            from workspace_sessions import get_draft
            self.respond(200,get_draft(annotation))

        def get_progress(self):
            data = progress.progress_response(self.headers.get('X-Progress-Revision'), self.server.chamber_videos.summaries())
            with self.server.outside_control():
                self.respond(200, data)

        def read_archive(self, sample):
            from workspace_sessions import archived_run
            with progress.lock:
                if progress.data['sample'] == sample and progress.data['status'] in ('preparing', 'running'):
                    raise ValueError('此任务仍在处理，请查看当前进度')
            return archived_run(root, sample)

        def get_saved_results(self):
            from workspace_sessions import chamber_summaries, saved_timeline
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            try:
                if route.path == '/api/history-result' or query.get('history') == ['1'] and route.path == '/api/saved-timeline':
                    from workspace_sessions import archived_result
                    sample = query.get('run', [''])[0]
                    with self.server.outside_control():
                        archive = self.read_archive(sample)
                        if route.path == '/api/history-result':
                            result = archived_result(*archive)
                        else:
                            result = saved_timeline(root, sample, trend_only=query.get('detail') != ['1'])
                        self.respond(200, result)
                    return
                with progress.lock:
                    items = progress.batch.group_items(query.get('group', [None])[0]) if progress.batch else []
                if route.path == '/api/saved-timeline':
                    sample = query.get('run', [''])[0]
                    with progress.lock:
                        active = progress.data['status'] == 'complete' and progress.data['sample'] == sample
                    if not active and not any(i['sample'] == sample and i['status'] == 'complete' for i in items):
                        raise ValueError('所选任务没有已完成的结果')
                    with self.server.outside_control():
                        self.respond(200, saved_timeline(root, sample, trend_only=query.get('detail') != ['1']))
                elif route.path == '/api/batch-results':
                    self.respond(200, dict(items=items, reports=chamber_summaries(root, items)))
                else:
                    self.respond(200, chamber_summaries(root, items))
            except (KeyError, OSError, TypeError, ValueError) as exc:
                self.respond(400, dict(error=str(exc)))

        def get_chamber_video(self):
            try:
                key = parse_qs(urlsplit(self.path).query).get('job', [''])[0]
                self.respond(200, self.server.chamber_videos.status(key))
            except ValueError as exc:
                self.respond(404, dict(error=str(exc)))

        def get_live_preview(self):
            try:
                query = parse_qs(urlsplit(self.path).query)
                run = query.get('run', [''])[0]
                sequence = int(query['sequence'][0]) if 'sequence' in query else None
                data = progress.preview(run, int(query.get('after', ['0'])[0]), sequence,
                                        focus=query.get('view') == ['focus'])
                self.respond(200, data, 'image/jpeg' if sequence is not None else 'application/json; charset=utf-8')
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.respond(409, dict(error=str(exc)))

        def get_frame_metrics(self):
            try:
                sample = parse_qs(urlsplit(self.path).query).get('run', [''])[0]
                self.respond(200, progress.frame_results(sample))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.respond(409, dict(error=str(exc)))

        def get_frame_preview(self):
            try:
                with progress.lock:
                    ready = progress.data['status'] in ('complete', 'failed', 'cancelled') and progress.config
                    run, config = progress.run_id, progress.config
                if not ready:
                    self.respond(409, dict(error='当前没有可重新标注的已结束任务')); return
                frame = int(parse_qs(urlsplit(self.path).query).get('frame', ['-1'])[0])
                with self.server.outside_control():
                    preview = self.server.preview_reader.read(config['video'], frame)
                    if preview.shape[1] > 1280:
                        preview = cv2.resize(preview, (1280, round(preview.shape[0]*1280/preview.shape[1])))
                    data = png(preview)
                with progress.lock:
                    changed = progress.run_id != run or progress.config is not config
                if changed:
                    self.respond(409, dict(error='结果会话已改变')); return
                with self.server.outside_control():
                    self.respond(200, data, 'image/png')
            except (ValueError, KeyError, OSError, cv2.error):
                self.respond(400, dict(error='无法读取指定帧，请重新选择'))

        def get_artifact(self):
            self.send_artifact()

        def get_image(self):
            annotation = self.server.annotation
            self.respond(200, png(annotation.image), 'image/png')

        def get_state(self):
            annotation = self.server.annotation
            paths, saved = getattr(annotation, 'sequence', ([], []))
            self.respond(200, dict(annotation.initial_state(), progress_enabled=progress is not None,
                                   can_edit_mode=can_edit_annotation_mode(annotation, progress),
                                   batch_deferred=getattr(annotation, 'batch_deferred', False),
                                   chamber_mode=bool(getattr(annotation, "chamber_mode", False)),
                                   sequence_index=len(saved)+1, sequence_total=len(paths)))

        def get_algorithms(self):
            # Run outside /state and outside the single HTTP request thread.
            # Dependency checks must not freeze mask editing.
            from sam2_models import availability, single_worm_default
            annotation = self.server.annotation
            model = annotation.model_size if annotation else single_worm_default(root)
            probes = self.server.model_probes
            if (parse_qs(urlsplit(self.path).query).get('retry')==['1']
                    and not probes.get(model,{}).get('checking')):
                probes.pop(model,None)
            if model not in probes:
                probes[model] = dict(available=False, checking=True, detail='正在检查分析组件…')
                def probe():
                    try:
                        probes[model] = dict(availability(model), checking=False)
                    except Exception as exc:
                        probes[model] = dict(available=False, checking=False, detail=f'分析组件检查失败：{exc}')
                threading.Thread(target=probe, daemon=True).start()
            self.respond(200, dict(algorithm=probes[model]))

        def post_chamber_video(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1048576: raise ValueError('视频导出请求无效')
            body = json.loads(self.rfile.read(size))
            from chamber_videos import members_for_video
            group, sample = body.get('group'), body.get('sample')
            single = body.get('single') is True
            with progress.lock:
                if group and progress.batch:
                    items = progress.batch.group_items(group)
                elif single and progress.data['status'] == 'complete' and progress.data['sample'] == sample:
                    config = progress.config
                    items = [dict(sample=sample, video_path=config['video'], chamber_id=config.get('chamber_id'), status='complete')]
                else:
                    raise ValueError('请打开对应的培养槽结果')
            members, missing = members_for_video(items, sample, partial=bool(body.get('missing')), single=single)
            if sorted(missing) != sorted(body.get('missing', [])):
                raise ValueError('培养槽完成状态已改变，请重新选择导出')
            job = self.server.chamber_videos.start(group or '', members, missing, full=not single)
            self.respond(202, dict(job=job)); return

        def post_channel_task(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            size = int(self.headers.get('Content-Length', '0'))
            batch = urlsplit(self.path).path == '/api/channel/batch'
            if not 0 < size <= (4*1024*1024 if batch else 8192): raise ValueError('计数请求无效')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict): raise ValueError('计数请求无效')
            counts = self.server.channel_counts()
            if batch:
                with self.server.outside_control():
                    self.respond(200, counts.batch.action(body))
            else:
                self.respond(200, counts.action(body.get('file'), body.get('action'), body))
            return

        def post_batch(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192: raise ValueError('队列请求无效')
            body = json.loads(self.rfile.read(size))
            with progress.lock:
                if progress.batch is None: raise ValueError('请使用 Worm Studio 启动批处理')
                action = body.get('action')
                if action == 'stop':
                    if not progress.batch.running: raise ValueError('批处理尚未启动')
                    progress.cancel_requested.set()
                    if progress.batch.active is None: progress.batch.running = False
                else:
                    if progress.idle_run() is None:
                        raise ValueError('请先保存当前标注或等待当前处理结束')
                    if action == 'start': progress.batch.start()
                    elif action == 'retry-dispatch': progress.batch.retry_dispatch()
                    elif action == 'remove': progress.batch.remove(body.get('id'))
                    elif action == 'retry': progress.batch.retry(body.get('id'))
                    elif action == 'retry-one': progress.batch.retry(body.get('id'), start=True, group=body.get('group'))
                    else: raise ValueError('未知队列操作')
            self.respond(200, progress.snapshot()); return

        def post_layout(self):
            annotation = self.server.annotation
            token = progress.token if progress else annotation.token if annotation else None
            if not token or self.headers.get('X-Annotation-Token') != token:
                self.respond_before_body(403, dict(error='Invalid layout session')); return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32768: raise ValueError('布局请求过大或为空')
            from workspace_layout import update
            self.respond(200, update(root, json.loads(self.rfile.read(size)))); return

        def post_edit_preview(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192: raise ValueError('掩膜修正请求无效')
            body = json.loads(self.rfile.read(size))
            from mask_correction import request_edit
            result = request_edit(progress, root, body.get('run'), body.get('sequence'), annotation_factory=partial(annotation_type, root=root))
            self.respond(202 if result.get('waiting') else 200, result); return

        def post_library(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403,dict(error='Invalid processing session')); return
            from workspace_library import upload, select, organize
            size = int(self.headers.get('Content-Length', '0'))
            if self.path == '/api/library-tree':
                if not 0 < size <= 32768: raise ValueError('文件树请求过大或为空')
                result=organize(root,json.loads(self.rfile.read(size)))
            elif self.path == '/api/library-link':
                if not 0 < size <= 8192: raise ValueError('视频路径无效')
                from workspace_library import register, VIDEO
                path = Path(json.loads(self.rfile.read(size))['path']).expanduser()
                if not path.is_absolute() or path.suffix.lower() not in VIDEO:
                    raise ValueError('请选择视频文件的完整路径')
                result = register(root, path)
            elif self.path == '/api/library-selection':
                if not 0 < size <= 8192: raise ValueError('文件选择请求无效')
                result = select(root, json.loads(self.rfile.read(size)).get('id'))
            else:
                q = parse_qs(urlsplit(self.path).query)
                result = upload(root, self.rfile, size, q.get('name',[''])[0], q.get('relativePath',[''])[0], q.get('signature',[''])[0],q.get('parent',[None])[0],
                                transfer=self.server.outside_control)
            self.respond(200,result); return

        def post_history(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192: raise ValueError('任务请求无效')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict): raise ValueError('任务请求无效')
            from workspace_sessions import remove_history_entry
            remove_history_entry(root, body.get('sample'))
            self.respond(200, dict(removed=True)); return

        def post_result_action(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403,dict(error='Invalid processing session')); return
            if self.path == '/api/cancel':
                progress.cancel()
                self.respond_before_body(200,dict(cancelling=True));return
            with progress.lock:
                run = progress.idle_run()
                config = progress.config
            if run is None: raise ValueError('请先结束当前任务或停止批处理')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=8192: raise ValueError('任务请求无效')
            body=json.loads(self.rfile.read(size))
            from workspace_sessions import reopen,load_config
            sample = body.get('sample')
            if self.path=='/api/open-run': reopen(progress,root,sample,expected_run=run)
            elif self.path=='/api/refresh-video':
                progress.queue_result(sample, run)
            else:
                if self.path in ('/api/confirm-head','/api/auto-head','/api/remove-head','/api/review-identity','/api/remove-identity-review'):
                    if not config or sample != config['sample']:
                        raise ValueError('当前结果已改变，请重新加载后复核')
                    from workspace_sessions import confirm_head
                    if self.path=='/api/auto-head':
                        from workspace_sessions import derive_geometry_run
                        _,settings=load_config(root,body.get('sample'))
                        settings['head_detection']=body.get('head_mode','twardis')
                        if settings['head_detection'] not in ('twardis','taper_seed','manual_seed'):raise ValueError('头尾初始化选项无效')
                        settings=derive_geometry_run(root,body.get('sample'),settings)
                    elif self.path=='/api/review-identity':
                        from workspace_sessions import review_identity
                        settings=review_identity(root,body.get('sample'),body.get('link_id'),body.get('decision'),body.get('reviewer'),body.get('evidence'))
                    elif self.path=='/api/remove-identity-review':
                        from workspace_sessions import remove_identity_review
                        settings=remove_identity_review(root,body.get('sample'),body.get('review_id'))
                    elif self.path=='/api/confirm-head':
                        settings=confirm_head(root,body.get('sample'),body.get('worm_id'),body.get('frame'),body.get('endpoint'))
                    else:
                        from workspace_sessions import remove_head
                        settings=remove_head(root,body.get('sample'),body.get('worm_id'),body.get('anchor_id'))
                else: _,settings=load_config(root,body.get('sample'))
                progress.queue_result(sample, run, settings)
            self.respond(200,dict(ok=True));return

        def post_annotation_action(self):
            annotation = self.server.annotation
            if self.headers.get('X-Annotation-Token')!=annotation.token:
                self.respond_before_body(403,dict(error='Invalid annotation session'));return
            if annotation.done.is_set(): raise ValueError('标注已保存')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=64*1024*1024: raise ValueError('请求过大或为空')
            body=json.loads(self.rfile.read(size))
            if self.path=='/api/annotation-mode':
                change_annotation_mode(annotation, progress, body.get('chamber_mode'))
                self.respond(200, dict(ok=True));return
            if self.path=='/api/draft':
                from workspace_sessions import save_draft
                self.respond(200,save_draft(annotation,body));return
            if hasattr(annotation, 'correction_source'):
                raise ValueError('当前正在修正指定帧的掩膜，请保存后再选择其他帧')
            candidate=annotation_type(annotation.settings, frame=body.get('frame'), root=root)
            annotation.token = candidate.token
            annotation.release_predictor()
            # Keep the existing Event: collect_annotation is waiting on it.
            for key in ('image','frame','frame_seconds','identity','crop'):
                setattr(annotation,key,getattr(candidate,key))
            self.respond(200,dict(ok=True));return

        def post_evaluation(self):
            from evaluation_http import save
            save(self, progress)
            return

        def post_reannotate(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            with progress.lock:
                run = progress.idle_run()
                config = progress.config if progress.data['status'] != 'ready' else None
                start_on_submit = progress.batch is not None
            if run is None or not config:
                self.respond_before_body(409, dict(error='当前没有可重新标注的已结束任务')); return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192: raise ValueError('标注帧请求无效')
            frame = json.loads(self.rfile.read(size)).get('frame')
            if type(frame) is not int: raise ValueError('请选择整数帧号')
            from run_storage import fresh_sample
            settings = dict(config, sample=fresh_sample(config['sample'], root/'outputs'),
                            previous_sample=config['sample'], crop_xyxy=None, groups=[], review_intervals=[])
            candidate = annotation_type(settings, frame=frame, root=root)
            candidate.start_on_submit = start_on_submit
            progress.queue_video(candidate, expected_run=run)
            self.respond(200, dict(annotation_url='/')); return

        def post_open_video(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            run = progress.idle_run()
            if run is None:
                self.respond_before_body(409, dict(error='当前任务尚未结束')); return
            uploaded = None
            sequence = []
            size = int(self.headers.get('Content-Length', '0'))
            if self.path == '/api/new-video':
                if not 0 < size <= 2*1024*1024: raise ValueError('视频路径请求过大，请分批拖入')
                body = json.loads(self.rfile.read(size))
                video = Path(str(body.get('video_path', '')).strip().strip('"')).expanduser()
                if not video.is_absolute(): raise ValueError('请填写视频的完整路径')
                if 'sequence' in body:
                    if not isinstance(body['sequence'], list): raise ValueError('视频顺序无效')
                    sequence = list(dict.fromkeys(Path(p) for p in body['sequence']))
                    if not sequence or sequence[0] != video: raise ValueError('视频顺序无效')
                    for path in sequence:
                        if not path.is_absolute() or not path.is_file(): raise ValueError(f'无法读取视频：{path}')
            else:
                filename = parse_qs(urlsplit(self.path).query).get('name', [''])[0]
                filename = filename.replace('\\','/').rsplit('/',1)[-1]
                filename = re.sub(r'[<>:"|?*\x00-\x1f]', '_', filename).strip('. ')
                if reserved_filename(filename):
                    filename = 'video-' + filename
                if not filename or Path(filename).suffix.lower() not in ('.mp4','.avi','.mov','.mkv','.m4v','.mpg','.mpeg','.wmv'):
                    raise ValueError('请选择 MP4、AVI、MOV 等视频文件')
                if not 0 < size <= 8*1024**3: raise ValueError('视频为空或超过 8 GB，请改用完整路径导入')
                folder = root/'input_videos'/secrets.token_hex(8)
                folder.mkdir(parents=True)
                uploaded = video = folder/filename
                try:
                    from workspace_library import receive_file
                    with self.server.outside_control():
                        receive_file(video, self.rfile, size)
                    progress.require_idle(run)
                except Exception:
                    video.unlink(missing_ok=True); folder.rmdir(); raise
            try:
                seconds = body.get('preview_seconds') if self.path == '/api/new-video' else None
                candidate = annotation_type.from_video(video, root=root, preview_seconds=seconds)
                if self.path == '/api/new-video' and body.get('chamber_mode') is True:
                    candidate.chamber_mode = True
                    sequence = sequence or [video]
                if sequence: candidate.sequence = (sequence, [])
                from workspace_library import register
                deferred = bool(sequence) or (self.path == '/api/new-video' and body.get('batch') is True)
                with progress.lock:
                    progress.require_idle(run)
                    if deferred and progress.batch is None: raise ValueError('请使用 Worm Studio 启动批处理')
                    candidate.start_on_submit = bool(self.path == '/api/new-video' and body.get('edit_setup') is True
                                                     and progress.batch and not deferred)
                    library_file = register(root, video)
                    progress.queue_video(candidate, deferred=deferred, expected_run=run)
            except Exception:
                if uploaded:
                    uploaded.unlink(missing_ok=True); uploaded.parent.rmdir()
                raise
            self.respond(200, dict(annotation_url='/', video_name=video.name, file=library_file)); return

        def post_close(self):
            if self.headers.get('X-Annotation-Token') != progress.token:
                self.respond_before_body(403, dict(error='Invalid processing session')); return
            with progress.lock:
                idle = progress.idle_run() is not None
                if idle:
                    progress.closed.set()
            if not idle:
                self.respond_before_body(409, dict(error='处理仍在进行，请等待完成')); return
            self.respond_before_body(200, dict(closed=True))
            return

        def post_annotation_result(self):
            annotation = self.server.annotation
            if self.headers.get('X-Annotation-Token') != annotation.token:
                self.respond_before_body(403, dict(error='Invalid annotation session')); return
            size = int(self.headers.get('Content-Length', '0'))
            limit = 64 * 1024 * 1024 if self.path in ('/save', '/api/annotation-metrics') else 8192
            if not 0 < size <= limit: raise ValueError('标注请求过大或为空')
            if self.path == '/api/annotation-metrics':
                from annotation_metrics import measure_annotation
                from types import SimpleNamespace
                token = annotation.token
                frame = SimpleNamespace(frame=annotation.frame, image=annotation.image)
                with self.server.outside_control():
                    result = measure_annotation(frame, json.loads(self.rfile.read(size)))
                if self.server.annotation is not annotation or annotation.token != token:
                    self.respond(409, dict(error='标注会话已改变')); return
                self.respond(200, result); return
            body = json.loads(self.rfile.read(size))
            if self.path in ('/predict', '/api/prediction'):
                if annotation.done.is_set():
                    raise ValueError('本次标注已保存')
                job = self.server.submit_prediction(annotation, body)
                if job is None:
                    self.respond(409, dict(error='已有轮廓预测正在进行')); return
                key, token, future = job
                if self.path == '/api/prediction':
                    self.respond(202, dict(job=key)); return
                # Existing callers of /predict expect the completed mask.
                with self.server.outside_control():
                    result = future.result()
                if self.server.annotation is not annotation or token != annotation.token or annotation.done.is_set():
                    self.respond(409, dict(error='本次预测已失效，请重新点选')); return
                self.respond(200, result); return
            if annotation.done.is_set():
                raise ValueError('当前标注已提交')
            if getattr(annotation, 'chamber_mode', False):
                result = save_chambers(progress, annotation, body)
                self.respond(200, result)
                return
            result = annotation.save(body)
            from workspace_sessions import save_draft
            save_draft(annotation, dict(submitted=True))
            if progress:
                if getattr(annotation, 'start_on_submit', False):
                    progress.submit_annotation(annotation)
                    annotation.release_predictor()
                    result['progress_url'] = '/progress'
                elif getattr(annotation, 'batch_deferred', False):
                    with progress.lock:
                        try:
                            progress.batch.add(annotation.result)
                        except Exception:
                            annotation.done.clear()
                            raise
                        progress.config = copy.deepcopy(annotation.result)
                        progress.data.update(status='ready', detail='标注已加入队列，可继续标注下一个视频')
                    annotation.release_predictor()
                    if hasattr(annotation, 'sequence'):
                        with progress.lock:
                            result.update(advance_sequence(progress, annotation))
                    else:
                        result['queued'] = True
                else:
                    result['progress_url'] = '/progress'
            self.respond(200, result)

        static = {
            '/wormstudio-icon.png': 'wormstudio-icon.png',
            '/studio': 'studio.html',
            '/studio.css': 'studio.css',
            '/studio-clean.css': 'studio-clean.css',
            '/channel-counts.css': 'channel-counts.css',
            '/channel-counts.js': 'channel-counts.js',
            '/channel-comparison.js': 'channel-comparison.js',
            '/channel-batch.js': 'channel-batch.js',
            '/channel-review.js': 'channel-review.js',
            '/studio.js': 'studio.js',
            '/studio-results.js': 'studio-results.js',
            '/result-navigation.js': 'result-navigation.js',
            '/result-viewport.js': 'result-viewport.js',
            '/result-player.js': 'result-player.js',
            '/result-events.js': 'result-events.js',
            '/workspace-controls.js': 'workspace-controls.js',
            '/library-tree.js': 'library-tree.js',
            '/studio-evaluation.js': 'studio-evaluation.js',
            '/studio-evaluation.css': 'studio-evaluation.css',
            '/studio-embed.css': 'studio-embed.css',
            '/draft-autosave.js': 'draft-autosave.js',
            '/workspace-usability.css': 'workspace-usability.css',
            '/workspace-links.js': 'workspace-links.js',
            '/annotation.css': 'manual_worm_annotation.css',
            '/annotation.js': 'manual_worm_annotation.js',
            '/annotation-readiness.js': 'annotation-readiness.js',
            '/analysis-presentation.js': 'analysis-presentation.js',
            '/annotation-coordinates.js': 'annotation-coordinates.js',
            '/progress-poll.js': 'progress-poll.js',
        }

        workspace_static = {
            '/review': 'review_result.html',
            '/review.js': 'review_result.js',
            '/select-video': 'select_video.html',
            '/select-video.js': 'select_video.js',
            '/processing.css': 'processing.css',
            '/processing.js': 'processing.js',
            '/live-preview.js': 'live-preview.js',
            '/phenotype-results.js': 'phenotype-results.js',
            '/frame-metrics.js': 'frame-metrics.js',
        }

        get_shared_routes = {
            '/api/head-preview': get_head_preview,
            '/api/prediction': get_prediction_status,
            '/api/layout': get_layout,
            '/api/evaluation/catalog': get_evaluation,
            '/api/evaluation/task': get_evaluation,
            '/api/evaluation/frame': get_evaluation,
        }

        get_workspace_routes = {
            '/api/channel/batch': get_channel_view,
            '/api/channel/batch-export': get_channel_view,
            '/api/channel/state': get_channel_view,
            '/api/channel/comparison': get_channel_view,
            '/api/channel/setup': get_channel_view,
            '/api/channel/frames': get_channel_view,
            '/api/channel/frame-image': get_channel_view,
            '/api/channel/event': get_channel_view,
            '/api/channel/export': get_channel_view,
            '/api/library': get_library,
            '/api/library-media': get_library_media,
            '/api/projects': get_projects,
            '/api/identity-links': get_identity,
            '/api/identity-frame': get_identity,
            '/api/head-anchors': get_head_anchors,
            '/api/progress': get_progress,
            '/api/chamber-summaries': get_saved_results,
            '/api/batch-results': get_saved_results,
            '/api/saved-timeline': get_saved_results,
            '/api/history-result': get_saved_results,
            '/api/chamber-video': get_chamber_video,
            '/api/live-preview': get_live_preview,
            '/api/frame-metrics': get_frame_metrics,
            '/api/frame-preview': get_frame_preview,
            '/artifact': get_artifact,
        }

        get_annotation_routes = {
            '/api/draft': get_draft,
            '/image.png': get_image,
            '/state': get_state,
            '/api/algorithms': get_algorithms,
        }

        post_workspace_routes = {
            '/api/chamber-video': post_chamber_video,
            '/api/channel/batch': post_channel_task,
            '/api/channel/task': post_channel_task,
            '/api/batch': post_batch,
            '/api/edit-preview-mask': post_edit_preview,
            '/api/library-upload': post_library,
            '/api/library-selection': post_library,
            '/api/library-tree': post_library,
            '/api/library-link': post_library,
            '/api/remove-history': post_history,
            '/api/open-run': post_result_action,
            '/api/resume-run': post_result_action,
            '/api/refresh-video': post_result_action,
            '/api/confirm-head': post_result_action,
            '/api/auto-head': post_result_action,
            '/api/remove-head': post_result_action,
            '/api/review-identity': post_result_action,
            '/api/remove-identity-review': post_result_action,
            '/api/cancel': post_result_action,
            '/api/reannotate': post_reannotate,
            '/api/new-video': post_open_video,
            '/api/upload-video': post_open_video,
            '/close': post_close,
        }

        post_shared_routes = {
            '/api/layout': post_layout,
            '/api/evaluation/save': post_evaluation,
        }

        post_annotation_routes = {
            '/api/annotation-frame': post_annotation_action,
            '/api/draft': post_annotation_action,
            '/api/annotation-mode': post_annotation_action,
            '/save': post_annotation_result,
            '/predict': post_annotation_result,
            '/api/prediction': post_annotation_result,
            '/api/annotation-metrics': post_annotation_result,
        }

        @workspace_request
        def do_GET(self):
            route = urlsplit(self.path).path
            annotation = self.server.annotation
            if route == '/' or (progress and route == '/progress'):
                if progress and (route == '/progress' or annotation is None or annotation.done.is_set()):
                    return self.send_static('processing.html')
                if annotation:
                    return self.send_static('manual_worm_annotation.html')
            name = self.static.get(route) or (self.workspace_static.get(route) if progress else None)
            if name:
                return self.send_static(name)
            action = self.get_shared_routes.get(route)
            if progress and not action:
                action = self.get_workspace_routes.get(route)
            if annotation and not action:
                action = self.get_annotation_routes.get(route)
            if action:
                return action(self)
            self.respond(404, {})

        @workspace_request
        def do_POST(self):
            route = self.path
            if route.startswith(('/api/upload-video?', '/api/library-upload?')):
                route = urlsplit(route).path
            try:
                action = self.post_shared_routes.get(route)
                if progress and not action:
                    action = self.post_workspace_routes.get(route)
                if self.server.annotation and not action:
                    action = self.post_annotation_routes.get(route)
                if action:
                    return action(self)
                self.respond_before_body(404, {})
            except (ValueError, KeyError, TypeError) as error:
                if self.server.closing:
                    return
                self.respond(400, dict(error=str(error)))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            except Exception as error:
                label = 'SAM2 预测失败' if self.path in ('/predict', '/api/prediction') else '请求处理失败'
                print(f'{self.path} {label}：{error}', flush=True)
                self.respond(500, dict(error=f'{label}：{error}'))

    class WorkspaceServer(WorkspaceHTTPServer):
        annotation = None
        counts = None
        chamber_videos = None

        def channel_counts(self):
            if self.counts is None:
                from channel_counts import ChannelCounts
                self.counts = ChannelCounts(root)
            return self.counts

        def server_close(self):
            super().server_close()
            if self.annotation is not None:
                self.annotation.release_predictor()
            if self.counts is not None:
                self.counts.close()
            if self.chamber_videos is not None:
                self.chamber_videos.pool.shutdown(wait=True, cancel_futures=True)

    server = WorkspaceServer(('127.0.0.1', 0), Handler)
    from chamber_videos import ChamberVideos
    server.chamber_videos = ChamberVideos(root)
    server.annotation = annotation
    server.model_probes = {}
    return server
