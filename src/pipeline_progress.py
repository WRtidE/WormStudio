"""Local processing status and opt-in, throttled child-process progress events."""
import copy
import json
import os
from pathlib import Path
import secrets
import threading
import time


PHASE_LABELS = dict(tracking='SAM2 掩膜跟踪', cleanup='掩膜检查与清理', quality='多虫接触与身份检查', fit='中线提取',
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
    label = PHASE_LABELS[stage['phase']]
    if name.startswith('niu-'): label = 'NIU 骨架提取'
    elif name.startswith('tapnext-'): label = 'TAPNext++ 双向点跟踪与时序融合'
    elif name.startswith('topology-'): label = '自交分支检查与保守恢复'
    elif name.startswith('hybrid-'): label = 'NIU / WormTracer 路径选择'
    if '-worm-' in name: label += ' · 线虫 ' + name.rsplit('-', 1)[1]
    return label


class PipelineProgress:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.run_id = secrets.token_hex(8)
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.cancel_requested = threading.Event()
        self.next_ready = threading.Event()
        self.next_settings = None
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

    def bind_server(self, server):
        self.server = server
        self.worker = threading.Thread(target=server.serve_forever, daemon=True)
        self.worker.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/progress'

    def configure(self, config, info, stages, base, device=''):
        from centerline_methods import METHODS
        base.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.cancel_requested.clear()
            self.config = copy.deepcopy(config)
            self.started = time.time()
            self.ended = None
            self.artifacts = {}
            self.frame_metrics = None
            self.path = base / 'pipeline-progress.json'
            self.data.update(status='running', sample=config['sample'], video_name=Path(config['video']).name,
                method=METHODS[config['centerline_method']]['label'], device=device,
                frames=info['frames'], fps=info.get('fps'), worm_count=len(info['ids']), output_dir=str(base.resolve()),
                active=None, error=None, artifacts=[], results=[], quality=None, phenotypes=None, logs='', notice='',
                stages=[dict(name=s['name'], label=stage_label(s), phase=s['phase'], status='pending',
                             fraction=0, current=None, total=None, unit='帧', detail='', seconds=0) for s in stages])
            self.persist()

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
            row['seconds'] = round(time.time()-self.active_started, 1)
            self.persist()

    def done(self, index, reused=False):
        with self.lock:
            row = self.data['stages'][index]
            row.update(status='reused' if reused else 'complete', fraction=1)
            if row['total'] is not None: row['current'] = row['total']
            self.data['active'] = None
            self.persist()

    def finish(self, base, config, stop_after):
        from centerline_methods import method_source
        with self.lock:
            self.ended = time.time()
            self.data.update(status='complete', detail='全部处理完成' if stop_after == 'export' else f'已完成所选阶段：{PHASE_LABELS[stop_after]}', active=None)
            result = base / ('phenotypes-' + method_source(config['centerline_method']))
            # Only expose outputs whose producing stage completed in this run.
            finished = {s['phase'] for s in self.data['stages'] if s['status'] in ('complete', 'reused')}
            outputs = [('video', '标注视频', result / f'{config["sample"]}_annotated_full-frame.mp4', 'export'),
                       ('source-video', '原始视频（只读）', Path(config['video']), 'metrics'),
                       ('metrics', '逐帧指标 CSV', result/'per_frame_metrics.csv', 'metrics'),
                       ('summary', '线虫汇总 CSV', result/'worm_summary.csv', 'metrics'),
                       ('events', '单向摆动事件 CSV', result/'bend_events.csv', 'metrics'),
                       ('plots', '指标与质心轨迹 PNG', result/'phenotype-plots.png', 'metrics'),
                       ('lines', '正式中线 NPZ', result/'centerlines.npz', 'metrics'),
                       ('display-lines', '显示中线 NPZ（含待复核候选）', result/'display-centerlines.npz', 'metrics'),
                       ('report', '分析报告', result/'报告.md', 'metrics'),
                       ('quality', '逐帧质量与证据 JSON', result/'quality.json', 'metrics'),
                       ('quality-audit', '质量门控汇总 JSON', result/'quality-audit.json', 'metrics'),
                       ('quality-gates', '逐帧门控与触发位置 CSV', result/'quality-gates.csv', 'metrics'),
                       ('tracklets', '匿名连续片段与边界 JSON', result/'tracklets.json', 'metrics'),
                       ('identity-links', '片段身份关联、图像依据与复核 JSON', result/'identity-links.json', 'metrics'),
                       ('tracklet-metrics', '片段局部指标 CSV（非确认个体轨迹）', result/'tracklet-metrics.csv', 'metrics'),
                       ('frame-json', '逐帧指标 JSON', result/'per_frame_metrics.json', 'metrics'),
                       ('diagnostics', '诊断候选指标（不作测量）', result/'diagnostic-metrics.json', 'metrics'),
                       ('diagnostic-lines', '诊断中线 NPZ（不作测量）', result/'diagnostic-centerlines.npz', 'metrics')]
            for key, label, path, phase in outputs:
                if phase in finished and path.is_file():
                    self.artifacts[key] = path
                    self.data['artifacts'].append(dict(key=key, label=label, filename=path.name))
            if 'summary' in self.artifacts:
                import csv
                import math
                fields = ('worm_id', 'frames', 'valid_frames', 'mask_valid_frames', 'median_length_um', 'mean_speed_um_s',
                          'identity_valid_frames','trajectory_valid_frames')
                with self.artifacts['summary'].open(encoding='utf-8-sig', newline='') as handle:
                    rows = []
                    for row in csv.DictReader(handle):
                        clean = {}
                        for key in fields:
                            if key not in row and key in ('identity_valid_frames','trajectory_valid_frames'):continue
                            try: value = float(row.get(key, ''))
                            except (ValueError, TypeError): value = None
                            clean[key] = value if value is not None and math.isfinite(value) else None
                        rows.append(clean)
                    self.data['results'] = rows
                    self.data['quality'] = summarize_quality(rows)
                    if self.data['quality']['needs_review']:
                        self.data['detail'] = '计算已完成，部分线虫结果需要复核。'
                from result_metrics import phenotype_results
                self.data['phenotypes'] = phenotype_results(result)
                if self.data.get('fps') is None:
                    self.data['fps'] = self.data['phenotypes'].get('fps')
            self.persist()

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
            return dict(self.frame_metrics, sample=sample, crop_xyxy=self.config.get('crop_xyxy') if self.config else None,
                        head_detection_mode=self.config.get('head_detection','twardis') if self.config else None)

    def queue_video(self, annotation):
        """Accept a validated next video while keeping the local server alive."""
        with self.lock:
            if self.data['status'] not in ('ready', 'complete', 'failed', 'cancelled') or self.next_ready.is_set():
                raise ValueError('当前处理尚未结束，请等待完成后选择新视频')
            self.path = self.event_path = self.log_path = None
            self.started = self.ended = None
            self.run_id = secrets.token_hex(8)
            self.artifacts = {}
            self.frame_metrics = None
            self.config = copy.deepcopy(annotation.settings)
            self.data.update(status='preparing', sample=annotation.settings['sample'], video_name=Path(annotation.video).name,
                             stages=[], active=None, error=None, artifacts=[], results=[], quality=None, phenotypes=None, logs='', notice='',
                             frames=annotation.n, worm_count=0, output_dir='', detail='请完成新视频的标注')
            self.server.annotation = annotation
            self.next_settings = annotation.settings
            self.next_ready.set()

    def fail(self, error, cancelled=False):
        with self.lock:
            self.ended = time.time()
            self.data.update(status='cancelled' if cancelled else 'failed', error=str(error) or '用户中断了处理',
                             detail='处理已停止，已完成的文件会保留。')
            index = self.data['active']
            if index is not None: self.data['stages'][index]['status'] = 'cancelled' if cancelled else 'failed'
            self.persist()

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.data)
            result['elapsed_seconds'] = round((self.ended or time.time())-self.started, 1) if self.started else 0
            rows = result['stages']
            result['completed_stages'] = sum(s['status'] in ('complete', 'reused') for s in rows)
            result['percent'] = round(100*sum(s['fraction'] for s in rows)/len(rows), 1) if rows else 0
            result['token'] = self.token
            result['can_reannotate'] = bool(self.config and self.config.get('video') and not self.data.get('read_only'))
            return result

    def persist(self):
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
        print(f'结果与进度：{self.url}；页面点击“完成并退出”，或终端按 Ctrl+C 结束。', flush=True)
        try:
            while not self.closed.wait(.2):
                if self.next_ready.is_set():
                    settings = self.next_settings
                    self.next_settings = None
                    self.next_ready.clear()
                    return settings
        except KeyboardInterrupt: pass

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(timeout=2)

