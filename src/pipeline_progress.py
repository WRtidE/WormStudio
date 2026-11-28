"""Local processing status and opt-in, throttled child-process progress events."""
from live_preview import discard_preview

import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time


PHASE_LABELS = dict(tracking='虫体识别与跟踪', cleanup='轮廓检查与清理', quality='身份与接触检查', fit='姿态分析',
                    repair='交叉分支修正', temporal='前后帧时序修正', metrics='表型指标计算', export='视频导出与校验')
_last_event = 0.


def summarize_quality(rows):
    """Summarize existing validity flags without introducing new cutoffs."""
    report = []
    for row in rows:
        total = row.get('frames')
        item = dict(worm_id=row.get('worm_id'), frames=total, issues=[])
        dimensions=[('mask_valid_frames', '掩膜'), ('valid_frames', '姿态')]
        if 'identity_valid_frames' in row:dimensions.append(('identity_valid_frames','身份连续性'))
        for key, label in dimensions:
            valid = row.get(key)
            known = total is not None and total > 0 and valid is not None and 0 <= valid <= total
            item[key] = valid
            item[key + '_ratio'] = valid / total if known else None
            if not known:
                item['issues'].append(f'{label}有效帧信息缺失')
            elif valid == 0:
                item['issues'].append(f'没有可用的{label}帧')
            elif valid < total:
                item['issues'].append(f'部分{label}帧未通过质量筛选')
        item['needs_review'] = bool(item['issues'])
        report.append(item)
    return dict(worms=report, total=len(report), needs_review=sum(r['needs_review'] for r in report))


def completed_result(base, config, phases, stop_after):
    from run_storage import result_artifacts
    from centerline_methods import method_source
    artifacts = result_artifacts(base, config, phases)
    detail = '全部处理完成' if stop_after == 'export' else f'已完成所选阶段：{PHASE_LABELS[stop_after]}'
    result = dict(status='complete', detail=detail, active=None, results=[], quality=None, phenotypes=None,
                  artifacts=[dict(key=key, label=label, filename=path.name) for key, (label, path) in artifacts.items()])
    paths = {key: path for key, (_, path) in artifacts.items()}
    if 'summary' in paths:
        import csv
        from measurement_units import number
        from result_metrics import phenotype_results
        fields = ('worm_id', 'frames', 'valid_frames', 'mask_valid_frames', 'median_length_um', 'mean_speed_um_s',
                  'identity_valid_frames', 'trajectory_valid_frames')
        with paths['summary'].open(encoding='utf-8-sig', newline='') as handle:
            rows = [{key: number(row.get(key)) for key in fields
                     if key in row or key not in ('identity_valid_frames', 'trajectory_valid_frames')}
                    for row in csv.DictReader(handle)]
        result['results'] = rows
        result['quality'] = summarize_quality(rows)
        if result['quality']['needs_review']:
            result['detail'] = '计算已完成，部分线虫结果需要复核。'
        result['phenotypes'] = phenotype_results(base / ('phenotypes-' + method_source(config['centerline_method'])))
        if config.get('chamber_id') is not None:
            result['phenotypes'].update(chamber_id=config['chamber_id'], video_name=Path(config['video']).name)
    return paths, result


def report_progress(current=None, total=None, detail='', unit='帧', *, fraction=None, force=False):
    """Report observed work, never time-based synthetic progress. No-op outside a run."""
    global _last_event
    target = os.environ.get('WORM_PROGRESS_FILE')
    if not target:
        return
    now = time.monotonic()
    if not force and now-_last_event < .4 and current != total:
        return
    event = dict(current=current, total=total, detail=detail, unit=unit, fraction=fraction)
    try:
        path = Path(target)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(event, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
        _last_event = now
    except OSError:
        pass  # Status reporting must never change scientific computation.


def stage_label(stage):
    name = stage['name']
    if name == 'crop-recovery': return '触边扩框与重跟踪'
    label = PHASE_LABELS[stage['phase']]
    if '-worm-' in name: label += ' · 线虫 ' + name.rsplit('-', 1)[1]
    return label


class PipelineProgress:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.run_id = secrets.token_hex(8)
        self.lock = threading.RLock()
        self.revision = 0
        self.closed = threading.Event()
        self.cancel_requested = threading.Event()
        self.next_ready = threading.Event()
        self.next_settings = None
        self.worker_annotation = None
        self.server = self.worker = None
        self.path = self.event_path = self.log_path = None
        self.started = self.ended = None
        self.active_started = None
        self.config = None
        self.data = dict(status='preparing', sample='', video_name='', method='', device='',
                         frames=0, worm_count=0, stages=[], active=None, error=None, artifacts=[],
                         output_dir='', detail='正在检查配置并准备处理…', logs='', notice='', results=[], quality=None, phenotypes=None)
        self.artifacts = {}
        self.frame_metrics = None
        self.preview_dir = None
        self._owned_preview = None
        self.batch = None

    def bind_server(self, server):
        self.server = server
        self.worker = threading.Thread(target=server.serve_forever, daemon=True)
        self.worker.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/progress'

    def configure(self, config, info, stages, base, device='', *, notice=''):
        base.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.config = copy.deepcopy(config)
            self.started = time.time()
            self.ended = None
            self.artifacts = {}
            self.frame_metrics = None
            self.run_id = secrets.token_hex(8)
            self.preview_dir = base / 'live-preview' / self.run_id
            retired, self._owned_preview = self._owned_preview, self.preview_dir
            self.path = base / 'pipeline-progress.json'
            self.data.update(status='running', sample=config['sample'], video_name=Path(config['video']).name,
                history_reopened=False, saved_elapsed_seconds=None, saved_finished_at=None,
                method='自动分析', device=device,
                frames=info['frames'], fps=info.get('fps'), worm_count=len(info['ids']), output_dir=str(base.resolve()),
                active=None, error=None, artifacts=[], results=[], quality=None, phenotypes=None, logs='', notice=notice,
                stages=[dict(name=s['name'], label=stage_label(s), phase=s['phase'], status='pending',
                             fraction=0, current=None, total=None, unit='帧', detail='', seconds=0) for s in stages])
            self.persist()
        discard_preview(retired)

    def begin(self, index, log):
        with self.lock:
            self.data['active'] = index
            self.data['detail'] = '正在加载模型与输入数据…'
            self.data['logs'] = ''
            self.active_started = time.time()
            self.log_path = log
            self.event_path = log.parent / f'{self.run_id}-{index}.progress.json'
            self.data['stages'][index].update(status='running', detail=self.data['detail'])
            self.persist()

    def poll(self):
        with self.lock:
            index = self.data['active']
            if index is None: return
            row = self.data['stages'][index]
            prior_row, prior_log = row.copy(), self.data['logs']
            if self.event_path and self.event_path.exists():
                try:
                    event = json.loads(self.event_path.read_text(encoding='utf-8'))
                    current, total = event.get('current'), event.get('total')
                    fraction = event.get('fraction')
                    if fraction is None and total and current is not None: fraction = current / total
                    row.update(current=current, total=total, unit=event.get('unit', '帧'),
                               detail=event.get('detail', ''))
                    if fraction is not None: row['fraction'] = max(row['fraction'], min(.99, max(0., float(fraction))))
                    self.data['detail'] = row['detail']
                except (OSError, ValueError, TypeError): pass
            if self.log_path and self.log_path.exists():
                try:
                    with self.log_path.open('rb') as handle:
                        handle.seek(max(0, self.log_path.stat().st_size-8000))
                        self.data['logs'] = '\n'.join(handle.read().decode('utf-8', errors='replace').splitlines()[-30:])
                except OSError: pass
            changed = row != prior_row or self.data['logs'] != prior_log
            row['seconds'] = round(time.time()-self.active_started, 1)
            if changed:
                self.persist()

    def done(self, index, reused=False):
        with self.lock:
            row = self.data['stages'][index]
            row.update(status='reused' if reused else 'complete', fraction=1)
            if row['total'] is not None: row['current'] = row['total']
            self.data['active'] = None
            self.persist()

    def finish(self, base, config, stop_after, *, saved=None, expected_run=None):
        """Publish a complete result, including its artifacts, in one transition."""
        with self.lock:
            run = self.run_id
            stages = self.data['stages'] if saved is None else saved['stages']
            phases = {s['phase'] for s in stages if s['status'] in ('complete', 'reused')}
        artifacts, result = completed_result(base, config, phases, stop_after)
        with self.lock:
            if saved is None and self.run_id != run:
                return None
            if saved is not None:
                if expected_run is not None:
                    self.require_idle(expected_run)
                self._prepare_sample(config, saved['frames'], result['detail'])
                self.data.update(saved)
                self.server.annotation = None
            if self.data.get('fps') is None and result.get('phenotypes'):
                self.data['fps'] = result['phenotypes'].get('fps')
            self.ended = time.time()
            self.artifacts = artifacts
            self.data.update(result)
            followup = self._finish_batch() if saved is None else None
            self.persist()
            return followup

    def _finish_batch(self):
        """Record the worker's terminal state before another request can replace it."""
        if self.batch is None:
            return None
        sample = self.data['sample']
        self.batch.finish(self.data['status'], self.data.get('error'), cancelled=self.cancel_requested.is_set(),
                          result_sample=sample, previous_sample=(self.config or {}).get('previous_sample'))
        if self.data['status'] == 'complete':
            group = self.batch.group_for_result(sample)
            members = self.batch.group_items(group) if group else []
            if any(i['sample'] == sample and i['status'] == 'complete' and i.get('chamber_id') for i in members):
                return group, sample, members
        return None

    def frame_results(self, sample):
        from frame_metrics import load_frame_metrics
        with self.lock:
            if sample != self.data['sample']:
                raise ValueError('结果会话已改变，请刷新页面')
            path = self.artifacts.get('metrics')
            if path is None:
                raise ValueError('本次任务尚未生成逐帧指标')
            if self.frame_metrics is None:
                self.frame_metrics = load_frame_metrics(path, self.data.get('fps'), self.data['frames'])
                summary=path.parent/'summary.json'
                if summary.is_file():
                    self.frame_metrics['crop_xyxy']=json.loads(summary.read_text(encoding='utf-8')).get('crop_xyxy')
            return dict(self.frame_metrics, sample=sample, crop_xyxy=self.frame_metrics.get('crop_xyxy') or (self.config.get('crop_xyxy') if self.config else None),
                        head_detection_mode=self.config.get('head_detection','twardis') if self.config else None)

    def _prepare_sample(self, settings, frames, detail):
        self.path = self.event_path = self.log_path = None
        self.started = self.ended = self.active_started = None
        self.run_id = secrets.token_hex(8)
        self.artifacts = {}
        self.frame_metrics = self.preview_dir = None
        self.config = copy.deepcopy(settings)
        self.data.update(status='preparing', sample=settings['sample'], video_name=Path(settings['video']).name,
                         history_reopened=False, legacy_video=False, read_only=False,
                         saved_elapsed_seconds=None, saved_finished_at=None,
                         stages=[], active=None, error=None, artifacts=[], results=[], quality=None, phenotypes=None,
                         frames=frames, fps=None, worm_count=0, device='', method='', output_dir='', logs='', notice='',
                         detail=detail)

    def idle_run(self):
        """Identify the idle session so a slow operation can recheck it at commit."""
        with self.lock:
            if (self.data['status'] in ('ready', 'complete', 'failed', 'cancelled')
                    and not self.closed.is_set() and not self.next_ready.is_set()
                    and not (self.worker_annotation is not None and self.cancel_requested.is_set())
                    and not (self.batch and self.batch.running)):
                return self.run_id
            return None

    def require_idle(self, expected_run):
        if expected_run is None or self.idle_run() != expected_run:
            raise ValueError('当前任务已改变或尚未结束，请重新选择')

    def _publish_next(self, settings):
        # Callers hold self.lock through the corresponding session transition.
        self.next_settings = settings
        self.next_ready.set()

    def queue_result(self, sample, expected_run, settings=None):
        with self.lock:
            self.require_idle(expected_run)
            if settings is None:
                if (sample != self.data['sample'] or not self.data.get('history_reopened')
                        or not self.data.get('legacy_video') or self.data.get('read_only')):
                    raise ValueError('请先打开需要更新视频的历史结果')
                source = self.artifacts.get('source-video')
                if source is None or not source.is_file():
                    raise ValueError('请先将原视频放回原来的位置')
                self._prepare_sample(self.config, self.data['frames'], '正在准备重新导出标注视频')
                command = {'_refresh_video': sample}
            else:
                self._prepare_sample(settings, 0, '正在恢复保存的任务')
                command = dict(settings, _resume_saved=True)
            self.server.annotation = None
            self.cancel_requested.clear()
            self._publish_next(command)

    def submit_annotation(self, annotation):
        with self.lock:
            if self.server.annotation is not annotation or self.data['status'] != 'preparing' or self.next_ready.is_set():
                raise ValueError('标注会话已改变，请重新加载')
            self.config = copy.deepcopy(annotation.result)
            command = copy.deepcopy(annotation.result)
            command['_resume_saved'] = True
            self._publish_next(command)

    def queue_video(self, annotation, deferred=False, *, expected_run=None):
        """Accept a validated next video while keeping the local server alive."""
        with self.lock:
            self.require_idle(self.run_id if expected_run is None else expected_run)
            self._prepare_sample(annotation.settings, annotation.n, '请完成新视频的标注')
            self.server.annotation = annotation
            annotation.batch_deferred = deferred
            self.cancel_requested.clear()
            if not deferred and not getattr(annotation, 'start_on_submit', False):
                self._publish_next(copy.deepcopy(annotation.settings))

    def cancel(self):
        with self.lock:
            status = self.data['status']
            if status not in ('running', 'preparing'):
                raise ValueError('当前没有正在运行的任务')
            self.cancel_requested.set()
            annotation = self.server.annotation
            if status == 'preparing' and (self.next_ready.is_set() or (annotation and not annotation.done.is_set())):
                self.next_settings = None
                self.next_ready.clear()
                if annotation:
                    annotation.result = None
                    annotation.done.set()
                self.fail('已取消准备；保存过的草稿仍可恢复', cancelled=True)

    def fail(self, error, cancelled=False, *, finish_batch=False):
        with self.lock:
            self.ended = time.time()
            self.data.update(status='cancelled' if cancelled else 'failed', error=str(error) or '用户中断了处理',
                             detail='处理已停止，已完成的文件会保留。')
            index = self.data['active']
            if index is not None: self.data['stages'][index]['status'] = 'cancelled' if cancelled else 'failed'
            if finish_batch:
                self._finish_batch()
            self.persist()

    def elapsed_seconds(self):
        if self.data.get('history_reopened'):
            return self.data.get('saved_elapsed_seconds')
        return round((self.ended or time.time()) - self.started, 1) if self.started else 0

    def progress_response(self, previous, chamber_videos):
        with self.lock:
            batch = self.batch
            # Session transitions and queue dispatch can happen between persisted stages.
            key = (self.run_id, self.revision, self.data['status'], self.data['sample'],
                   self.data['detail'], self.data['notice'], self.config and self.config.get('video'),
                   batch and (batch.revision, batch.running, batch.dispatch_error), chamber_videos)
            revision = hashlib.sha256(json.dumps(key).encode()).hexdigest()
            if previous == revision:
                active = self.data['active']
                seconds = self.data['stages'][active]['seconds'] if active is not None else None
                return dict(unchanged=True, revision=revision, elapsed_seconds=self.elapsed_seconds(),
                            stage_seconds=seconds)
            return dict(self.snapshot(), revision=revision, chamber_videos=chamber_videos)

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.data)
            result['batch'] = self.batch.snapshot() if self.batch else dict(running=False, items=[])
            historical = result.get('history_reopened', False)
            result['elapsed_seconds'] = self.elapsed_seconds()
            result['finished_at'] = result.get('saved_finished_at') if historical else self.ended
            rows = result['stages']
            result['completed_stages'] = sum(s['status'] in ('complete', 'reused') for s in rows)
            result['percent'] = round(100*sum(s['fraction'] for s in rows)/len(rows), 1) if rows else 0
            result['token'] = self.token
            result['preview_run'] = self.run_id if self.preview_dir else None
            result['video_path'] = self.config.get('video') if self.config else None
            result['can_reannotate'] = bool(self.config and self.config.get('video') and not self.data.get('read_only'))
            result['can_edit_preview_masks'] = bool(result['can_reannotate'] and self.preview_dir)
            return result

    def preview(self, run, after=0, sequence=None, *, focus=False):
        from live_preview import preview_batch, latest_sequence
        with self.lock:
            if not self.preview_dir or run != self.run_id:
                raise ValueError('预览任务已切换，请重新读取进度')
            if sequence is None:
                return dict(preview_batch(self.preview_dir, after), run=self.run_id,
                            status=self.data['status'])
            if not 1 <= sequence <= latest_sequence(self.preview_dir):
                raise ValueError('预览帧尚未生成')
            suffix = '.focus.jpg' if focus else '.jpg'
            return (self.preview_dir / f'{sequence:09d}{suffix}').read_bytes()

    def persist(self):
        self.revision += 1
        if self.path:
            record = self.snapshot()
            record.pop('token', None)
            try:
                temp = self.path.with_suffix('.tmp')
                temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                temp.replace(self.path)
            except OSError: pass

    def wait(self):
        if not self.server: return
        # The single analysis worker has finished using its previous editor.
        with self.lock:
            self.worker_annotation = None
        print(f'结果与进度：{self.url}；页面点击“完成并退出”，或终端按 Ctrl+C 结束。', flush=True)
        try:
            while not self.closed.wait(.2):
                with self.lock:
                    if self.batch and self.batch.running and not self.next_ready.is_set():
                        settings = self.batch.take()
                        if settings:
                            self.server.annotation = None
                            self.cancel_requested.clear()
                            self._prepare_sample(settings, 0, '正在启动队列任务')
                            return settings
                    if self.next_ready.is_set():
                        settings = self.next_settings
                        if not settings.get('_resume_saved') and not settings.get('_refresh_video'):
                            self.worker_annotation = self.server.annotation
                        self.next_settings = None
                        self.next_ready.clear()
                        return settings
        except KeyboardInterrupt: pass

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(timeout=2)
        discard_preview(self._owned_preview)
        self._owned_preview = None
