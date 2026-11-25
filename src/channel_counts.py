"""Independent WormTracker tasks indexed by workspace file ID."""
import base64
import io
import math
import re
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from channel_runs import ChannelBatch, new_run, read, save
from workspace_library import VIDEO, media, load_index
from wormtracker.config import WormTrackerConfig
from wormtracker.core.grid import get_interpolated_grid
from wormtracker.core.grid_reference import ChannelGridReference
from wormtracker.engine.mog2 import MOG2Engine


BASE = WormTrackerConfig.from_yaml(Path(__file__).parent/'wormtracker/defaults.yaml')
FIELDS = ('num_channels', 'mask_top_ratio', 'tripwire_ratio', 'mask_bottom_ratio')
DEFAULTS = {key: getattr(BASE, key) for key in FIELDS}


def settings(data):
    if not isinstance(data, dict):
        raise ValueError('通道参数无效')
    result = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    n = result['num_channels']
    if type(n) is not int or not 1 <= n <= 100:
        raise ValueError('通道数应为 1 至 100')
    ratios = [result[k] for k in FIELDS[1:]]
    if not all(type(v) in (int, float) for v in ratios) or not 0 <= ratios[0] < ratios[1] < ratios[2] <= 1:
        raise ValueError('检测线应位于两条检测边界之间')
    return result


def grid_for(frame, config):
    return get_interpolated_grid(frame, config.num_channels, config.x_margin, wall_ratio=config.wall_ratio,
                                 refine_walls=config.wall_refine, wall_peak_half_width=config.wall_peak_half_width)


def reference_for(source, config):
    cap = cv2.VideoCapture(str(source))
    try:
        # An opening title or a blank exposure should not become a permanent
        # calibration. Try a few frames from the same startup window as the background.
        window = max(1, config.init_frame_index)
        samples = set(np.linspace(0, window - 1, min(window, 5), dtype=int))
        for idx in range(window):
            if not cap.grab():
                break
            if idx not in samples:
                continue
            ok, frame = cap.retrieve()
            if not ok:
                continue
            try:
                grid = grid_for(frame, config)
                widths = np.diff(grid['channel_bounds'], axis=1).ravel()
                span = grid['channel_bounds'][-1][1] - grid['channel_bounds'][0][0]
                if (len(widths) != config.num_channels or widths.min() < 0.45 * np.median(widths)
                        or span < 0.7 * (grid['roi_right'] - grid['roi_left'])):
                    grid = get_interpolated_grid(frame, config.num_channels, config.x_margin,
                                                 wall_ratio=config.wall_ratio, refine_walls=False,
                                                 wall_peak_half_width=config.wall_peak_half_width)
                reference = ChannelGridReference(frame, grid, config.tripwire_ratio)
                reference.locate(frame)
                return reference
            except RuntimeError:
                continue
        return None
    finally:
        cap.release()


def locate_grid(frame, config, reference):
    if reference is None:
        return grid_for(frame, config)
    try:
        return reference.locate(frame)
    except RuntimeError:
        # A camera cut can invalidate the template. Require a complete, plausible
        # layout before resuming independent wall detection.
        grid = grid_for(frame, config)
        widths = np.diff(grid['channel_bounds'], axis=1).ravel()
        typical = reference.grid['channel_width']
        if len(widths) != config.num_channels or np.any((widths < typical * .55) | (widths > typical * 1.65)):
            raise RuntimeError('无法可靠定位通道壁')
        return grid


def startup_background(source, window):
    """Sample the opening window with a separate decoder so all frames remain countable."""
    cap = cv2.VideoCapture(str(source))
    samples = []
    try:
        length = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        window = max(1, min(window, length) if length > 0 else window)
        positions = set(np.linspace(0, window - 1, min(5, window), dtype=int))
        for i in range(window):
            if not cap.grab():
                break
            if i in positions:
                ok, frame = cap.retrieve()
                if ok:
                    samples.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    finally:
        cap.release()
    if not samples:
        raise ValueError('无法读取背景采样帧')
    stack = np.stack(samples)
    mid = len(samples) // 2
    stack.partition(mid, axis=0)
    return stack[mid]


def jpeg(frame, limit, quality, *, width_only=False):
    h, w = frame.shape[:2]
    scale = min(1, limit / (w if width_only else max(w, h)))
    if scale < 1:
        frame = cv2.resize(frame, (max(1, round(w*scale)), max(1, round(h*scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError('无法生成计数预览')
    return 'data:image/jpeg;base64,' + base64.b64encode(buf).decode('ascii')


def picture(frame, grid):
    h, w = frame.shape[:2]
    return dict(width=w, height=h, image=jpeg(frame, 1000, 78, width_only=True),
                bounds=[[int(a), int(b)] for a, b in grid['channel_bounds']])


def event_images(source, event, last_frame):
    """Decode consecutive source frames using a single seek and identical crop coordinates."""
    center = event['frame']
    start = max(1, event.get('prev_frame', center - 1))
    cap = cv2.VideoCapture(str(source))
    try:
        if not cap.isOpened() or (start > 1 and not cap.set(cv2.CAP_PROP_POS_FRAMES, start - 1)):
            raise ValueError('无法定位越线原帧')
        frames = []
        for number in range(start, min(last_frame, center + 1) + 1):
            ok, frame = cap.read()
            if not ok:
                raise ValueError(f'无法读取第 {number} 帧')
            if not frames:
                h, w = frame.shape[:2]
                left, right = event['bounds']
                cx, cy = (left + right) // 2, event['line_y']
                half_w = max(24, (right-left)*2, abs(cx-event['x'])+12, abs(cx-event['prev_x'])+12)
                half_h = max(48, (right-left)*4, abs(cy-event['y'])+24, abs(cy-event['prev_y'])+24)
                x1, x2 = max(0, cx-half_w), min(w, cx+half_w)
                y1, y2 = max(0, cy-half_h), min(h, cy+half_h)
            if number in (start, center, center + 1):
                frames.append(dict(frame=number, image=jpeg(frame[y1:y2, x1:x2], 640, 92)))
            if number == center:
                full = jpeg(frame, 1200, 85)
        return dict(width=w, height=h, image=full, crop=[x1, y1, x2, y2], frames=frames)
    finally:
        cap.release()


class ChannelFrameReader:
    """One sequential decoder for the visible channel video, independent of the counting thread."""
    def __init__(self):
        self.capture = None
        self.key = None

    def close(self):
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        self.key = None

    def batch(self, source, task, start, stop):
        key = (source, task['id'])
        if self.key != key:
            self.close()
            self.capture = cv2.VideoCapture(str(source))
            self.config = WormTrackerConfig.from_dict(dict(BASE.to_dict(), **task['settings']))
            self.reference = reference_for(source, self.config) if task.get('grid_registration') else None
            self.next_frame = 1
            self.event_index = 0
            self.counts = [0] * self.config.num_channels
            self.key = key
        events = task.get('events', ())
        if self.next_frame != start:
            if not self.capture.set(cv2.CAP_PROP_POS_FRAMES, start-1):
                raise ValueError('无法定位原视频帧')
            self.next_frame = start
            self.event_index = 0
            self.counts = [0] * self.config.num_channels
        frames = []
        for number in range(start, stop+1):
            ok, frame = self.capture.read()
            if not ok:
                raise ValueError(f'无法读取第 {number} 帧')
            self.next_frame = number+1
            try:
                grid = locate_grid(frame, self.config, self.reference)
            except RuntimeError:
                grid = {'channel_bounds': []}
            while self.event_index < len(events) and events[self.event_index]['frame'] <= number:
                self.counts[events[self.event_index]['channel']-1] += 1
                self.event_index += 1
            frames.append(dict(picture(frame, grid), frame=number, counts=self.counts.copy()))
        return frames


class ChannelCounts:
    def __init__(self, root):
        self.root = root
        self.condition = threading.Condition()
        self.jobs = {}
        self.closed = False
        self.frame_reader = ChannelFrameReader()
        self.reader_lock = threading.Lock()
        self.batch = ChannelBatch(self, DEFAULTS)

    def folder(self, file):
        if not isinstance(file, str) or not re.fullmatch('[0-9a-f]{32}', file):
            raise ValueError('请先选择已导入的视频')
        return self.root/'outputs'/'channel-counts'/file

    def source(self, file):
        self.folder(file)
        path = media(self.root, file)
        if not path or path.suffix.lower() not in VIDEO or not path.is_file():
            raise ValueError('原视频不可用')
        return path

    def review_source(self, file, state):
        source = self.source(file)
        stat = source.stat()
        if state.get('source_stamp') and state['source_stamp'] != [stat.st_size, stat.st_mtime_ns]:
            raise ValueError('原视频已改变，无法回看这次计数的原帧')
        return source

    def setup(self, file, n, index=0, run=None):
        task = self.completed(file, run) if run else None
        path = self.review_source(file, task) if task else self.source(file)
        config = WormTrackerConfig.from_dict(dict(BASE.to_dict(), **(task['settings'] if task else settings({'num_channels':n}))))
        cap = cv2.VideoCapture(str(path))
        try:
            if index < 0 or index and index >= cap.get(cv2.CAP_PROP_FRAME_COUNT):
                raise ValueError('视频帧超出范围')
            if index:
                cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = cap.read()
            if not ok:
                raise ValueError('无法读取视频首帧')
            try:
                reference = reference_for(path, config) if task is None or task.get('grid_registration') else None
                grid = locate_grid(frame, config, reference)
            except RuntimeError:
                grid = {'channel_bounds': []}
            return picture(frame, grid)
        finally:
            cap.release()

    @staticmethod
    def record_path(folder, run):
        if not isinstance(run, str) or not re.fullmatch(r'[0-9]{20}-[a-f0-9]{8}', run):
            raise ValueError('任务编号无效')
        path = folder/run/'state.json'
        if not path.is_file():
            raise ValueError('计数记录不存在')
        return path

    @staticmethod
    def hidden_history(folder):
        path = folder/'hidden-history.json'
        hidden = read(path) if path.exists() else []
        if not isinstance(hidden, list) or not all(isinstance(run, str) for run in hidden):
            raise ValueError('计数记录列表格式无效')
        return set(hidden)

    def state(self, file, run=None, include_history=True):
        folder = self.folder(file)
        with self.condition:
            job = self.jobs.get(file)
            active_run = job['state']['id'] if job else None
            if include_history or run is None:
                hidden = self.hidden_history(folder)
                hidden.discard(active_run)
                history = sorted((p for p in folder.glob('*/state.json') if p.parent.name not in hidden), reverse=True)
            path = self.record_path(folder, run) if run is not None else next(iter(history), None)
            task = read(path) if path else None
            if task and task['status'] in ('running','paused') and active_run != task['id']:
                task.update(status='interrupted', error='上次计数中断，可继续计数')
            result = dict(task=task)
            if include_history:
                draft = folder/'draft.json'
                result.update(draft=read(draft) if draft.exists() else DEFAULTS, history=[p.parent.name for p in history],
                              active_run=active_run)
            return result

    def prepare_batch(self, body):
        files = body.get('files')
        if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
            raise ValueError('请选择已保存的视频')
        shared = settings(body['settings']) if body.get('settings') is not None else None
        confirmed = body.get('settings_by_file')
        if 'settings_by_file' in body and (not isinstance(confirmed, dict) or set(confirmed) != set(files)
                or any(not isinstance(value, dict) or not DEFAULTS.keys() <= value.keys() for value in confirmed.values())):
            raise ValueError('请逐个确认全部视频的检测线')
        library = {f['id']: f for f in load_index(self.root)[0]['files'] if not f.get('removed')}
        rows = []
        for file in dict.fromkeys(files):
            draft = self.folder(file)/'draft.json'
            entry = library.get(file)
            if not entry or entry['kind'] != 'video':
                raise ValueError('批次包含未保存或已移除的视频')
            if file in self.jobs:
                raise ValueError(entry['name']+' 正在计数，请稍后再加入批次')
            path = Path(entry['path'])
            if not path.is_file():
                raise ValueError(entry['name']+' 的原视频不可用')
            stat = path.stat()
            if confirmed is not None:
                params = settings(confirmed[file])
            else:
                params = shared.copy() if shared is not None else settings(read(draft) if draft.exists() else DEFAULTS)
            rows.append(dict(file=file, name=entry.get('relativePath') or entry['name'], run=new_run(), settings=params,
                             source_stamp=[stat.st_size, stat.st_mtime_ns], status='pending', total=None, error=''))
        return rows

    def start_run(self, file, path, run, params):
        stat = path.stat()
        state = dict(source_stamp=[stat.st_size, stat.st_mtime_ns], id=run, file=file, name=path.name,
                     settings=params, status='running', frame=0, frames=0, counts=[0]*params['num_channels'], events=[])
        folder = self.folder(file)/run
        save(folder/'state.json', state)
        self.launch(path, folder, state)

    def resume_run(self, path, state):
        stat = path.stat()
        if state.get('source_stamp') != [stat.st_size, stat.st_mtime_ns]:
            raise ValueError('原视频已改变，请重新开始计数')
        state.update(status='running', error=None)
        folder = self.folder(state['file'])/state['id']
        save(folder/'state.json', state)
        self.launch(path, folder, state)

    def launch_batch_item(self, item):
        file, run = item['file'], item['run']
        if file in self.jobs:
            raise ValueError('这个视频正在执行另一次计数，请稍后重试')
        path = self.source(file)
        stat = path.stat()
        if [stat.st_size, stat.st_mtime_ns] != item['source_stamp']:
            raise ValueError('原视频已改变，请重新确认视频并建立新批次')
        if (self.folder(file)/run/'state.json').is_file():
            state = self.state(file, run, False)['task']
            if state.get('id') != run or state.get('file') != file or state.get('settings') != item['settings']:
                raise ValueError('计数检查点与批次记录不一致')
            if state['status'] != 'interrupted':
                raise ValueError('这条计数记录无法继续')
            self.resume_run(path, state)
        else:
            self.start_run(file, path, run, settings(item['settings']))

    def action(self, file, action, body):
        folder = self.folder(file)
        with self.condition:
            if action in ('remove-comparison', 'restore-comparison'):
                path = self.record_path(folder, body.get('run'))
                if read(path).get('status') != 'complete':
                    raise ValueError('只能整理已完成的计数结果')
                marker = path.with_name('comparison-hidden')
                if action == 'remove-comparison':
                    marker.touch()
                else:
                    marker.unlink(missing_ok=True)
                return {'removed': action == 'remove-comparison'}
            if action == 'remove-history':
                run = body.get('run')
                self.record_path(folder, run)
                job = self.jobs.get(file)
                if job and job['state']['id'] == run:
                    raise ValueError('计数结束后才能移除这条记录')
                hidden = self.hidden_history(folder)
                if run not in hidden:
                    hidden.add(run)
                    save(folder/'hidden-history.json', sorted(hidden))
                return {'removed': run}
            if action == 'draft':
                save(folder/'draft.json', settings(body.get('settings')))
            elif action == 'start':
                if file in self.jobs:
                    raise ValueError('这个视频正在计数')
                path = self.source(file)
                params = settings(body.get('settings'))
                if self.batch.reserved(file):
                    raise ValueError('这个视频已在计数批次中')
                save(folder/'draft.json', params)
                self.start_run(file, path, new_run(), params)
            elif action == 'resume' and file not in self.jobs:
                state = self.state(file, body.get('run'), False)['task']
                if not state or state['status'] != 'interrupted':
                    raise ValueError('这条记录无法继续计数')
                if self.batch.reserved(file):
                    raise ValueError('请在批量计数中继续这一批视频')
                self.resume_run(self.source(file), state)
            elif action in ('pause','resume'):
                job = self.jobs.get(file)
                if not job or job['state']['id'] != body.get('run'):
                    raise ValueError('计数任务已结束')
                job['paused'] = action == 'pause'
                state = job['state']
                state['status'] = 'paused' if job['paused'] else 'running'
                save(folder/state['id']/'state.json', state)
                self.condition.notify_all()
            else:
                raise ValueError('未知的计数操作')
            return self.state(file, body.get('run') if action in ('pause','resume') else None)

    def launch(self, path, folder, state):
        job = dict(paused=False, state=state)
        job['thread'] = threading.Thread(target=self.process, args=(path, folder, job), daemon=True)
        self.jobs[state['file']] = job
        try:
            job['thread'].start()
        except RuntimeError:
            self.jobs.pop(state['file'])
            raise

    def process(self, source, folder, job):
        state = job['state']
        cap = None
        idx, cooldown, valid = 0, 0, 0
        checkpoint = state['frame']
        grid = None
        baseline, last = None, 0
        debounce = {}
        events = []
        try:
            config = WormTrackerConfig.from_dict(dict(BASE.to_dict(), **state['settings']))
            counts = {i:0 for i in range(1,config.num_channels+1)}
            engine = MOG2Engine(config, retain_masks=False)
            cap = cv2.VideoCapture(str(source))
            if not cap.isOpened():
                raise ValueError('无法打开视频')
            length = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            fps = fps if fps > 0 and math.isfinite(fps) else None
            reference = reference_for(source, config)
            engine.prime(startup_background(source, config.init_frame_index))
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.closed or not job['paused'])
                    if self.closed:
                        break
                ok, frame = cap.read()
                if not ok:
                    if not idx or length > 0 and idx < length:
                        raise ValueError('视频解码提前结束')
                    break
                last_frame = frame
                idx += 1
                previous_events = len(events)
                try:
                    grid = locate_grid(frame, config, reference)
                except RuntimeError:
                    grid = {'channel_bounds': []}
                    engine.reset()
                    debounce.clear()
                else:
                    valid += 1
                    if idx >= config.init_frame_index and baseline is None:
                        baseline = grid['channel_width']
                    if baseline and abs(grid['channel_width'] - baseline) / baseline > config.grid_mutation_tolerance:
                        cooldown = config.cooldown_frames
                    if cooldown:
                        engine.warmup(frame, grid.get('offset_x', 0))
                    else:
                        result = engine.process_frame(frame, idx, grid, counts, debounce)
                        if result.crossing_events:
                            stamp = cap.get(cv2.CAP_PROP_POS_MSEC)
                            seconds = (idx-1) / fps if fps else None
                            if stamp > 0 and math.isfinite(stamp):
                                seconds = stamp / 1000
                            for event in result.crossing_events:
                                events.append(dict(event, id=len(events)+1, frame=idx, time_s=seconds))
                        if result.is_panic:
                            engine.reset()
                            debounce.clear()
                            engine.warmup(frame, grid.get('offset_x', 0))
                    if cooldown:
                        engine.reset()
                        debounce.clear()
                        cooldown -= 1
                now = time.monotonic()
                if idx >= checkpoint and (len(events) != previous_events or now-last >= .35 or idx == length):
                    snap = picture(frame, grid)
                    with self.condition:
                        state.update(frame=idx, frames=length, fps=fps, grid_registration=reference is not None, counts=list(counts.values()),
                                     events=events.copy(), picture=snap)
                        save(folder/'state.json', state)
                    last = now
            if not self.closed and not valid:
                raise ValueError('未能识别通道边界，请检查视频')
            if idx and idx >= checkpoint:
                final = dict(frame=idx, frames=length, fps=fps, grid_registration=reference is not None, counts=list(counts.values()),
                             events=events, picture=picture(last_frame, grid))
            else:
                final = {}
            if not self.closed and valid < idx:
                final['warning'] = f'有 {idx-valid} 帧未识别到通道边界，已跳过。'
            final['status'] = 'interrupted' if self.closed else 'complete'
        except Exception as exc:
            final = dict(status='failed', error=str(exc))
        finally:
            if cap is not None:
                cap.release()
        with self.condition:
            try:
                state.update(final)
                save(folder/'state.json', state)
            except OSError as exc:
                state.update(status='failed', error='计数结果保存失败：'+str(exc))
                raise
            finally:
                self.jobs.pop(state['file'], None)
                self.batch.finished(state)

    def completed(self, file, run):
        state = self.state(file, run, False)['task']
        if not state or state['status'] != 'complete':
            raise ValueError('计数尚未完成')
        return state

    def frames(self, file, run, start, limit):
        if not run or start < 1 or not 1 <= limit <= 8:
            raise ValueError('视频帧范围无效')
        with self.condition:
            job = self.jobs.get(file)
            if job and job['state']['id'] == run:
                # Events and counts are replaced together with each committed checkpoint.
                task = job['state'].copy()
            else:
                task = self.state(file, run, False)['task']
        stop = min(start+limit-1, task['frame'])
        if stop < start:
            return dict(frames=[])
        with self.reader_lock:
            try:
                source = self.review_source(file, task)
                return dict(frames=self.frame_reader.batch(source, task, start, stop))
            except Exception:
                self.frame_reader.close()
                raise

    def event(self, file, run, number):
        state = self.completed(file, run)
        events = state.get('events', [])
        if not 1 <= number <= len(events):
            raise ValueError('越线记录不存在')
        source = self.review_source(file, state)
        return event_images(source, events[number-1], state['frame'])

    def frame_image(self, file, run, number, reader):
        state = self.completed(file, run)
        if not 1 <= number <= state['frame']:
            raise ValueError('帧号超出本次计数范围')
        frame = reader.read(self.review_source(file, state), number - 1)
        ok, image = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 93])
        if not ok:
            raise ValueError('无法生成原分辨率画面')
        return image.tobytes()

    def comparison(self, include_removed=False):
        rows = []
        directory = self.root/'outputs'/'channel-counts'
        for folder in sorted(directory.glob('*')):
            if not re.fullmatch('[0-9a-f]{32}', folder.name):
                continue
            for path in sorted(folder.glob('*/state.json'), reverse=True):
                state = read(path)
                if state.get('status') == 'complete':
                    removed = path.with_name('comparison-hidden').is_file()
                    if include_removed or not removed:
                        row = dict(file=folder.name, run=state['id'], name=state['name'], counts=state['counts'])
                        if include_removed:
                            row['removed'] = removed
                        rows.append(row)
                    break
        return rows

    def export_batch(self, batch_id):
        with self.condition:
            if not batch_id or self.batch.record['id'] != batch_id:
                raise ValueError('批次已改变，请刷新队列后重新导出')
            rows = [row.copy() for row in self.batch.record['items']]
            paused = {file for file, job in self.jobs.items() if job['paused']}
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = '批量通道计数'
        max_channels = max(row['settings']['num_channels'] for row in rows)
        sheet.append(['视频','状态','通道数','上边界（%）','计数线（%）','下边界（%）','累计通过','说明','计数记录']
                     + [f'通道 {n:02d}' for n in range(1, max_channels+1)])
        labels = dict(complete='已完成', failed='失败', running='处理中', interrupted='中断', pending='等待计数', skipped='已取消')
        for index, row in enumerate(rows, start=2):
            config = row['settings']
            counts = []
            if row['status'] == 'complete':
                counts = read(self.folder(row['file'])/row['run']/'state.json')['counts']
            label = '计数已暂停' if row['status']=='running' and row['file'] in paused else labels[row['status']]
            sheet.append([row['name'], label, config['num_channels'],
                          *[round(100*config[k], 2) for k in FIELDS[1:]],
                          sum(counts) if counts else None, row['error'], row['run'], *counts])
            for col in (1,8,9):
                sheet.cell(index, col).data_type = 's'
        sheet.freeze_panes = 'C2'
        sheet.auto_filter.ref = sheet.dimensions
        for column, width in [('A',36),('B',14),('H',45),('I',32)]:
            sheet.column_dimensions[column].width = width
        out = io.BytesIO()
        book.save(out)
        return out.getvalue()

    def export(self, file, run, channel=None):
        state = self.completed(file, run)
        if channel is not None and not 1 <= channel <= len(state['counts']):
            raise ValueError('通道编号超出范围')
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = '通道计数'
        sheet.append(['视频', state['name']])
        sheet['B1'].data_type = 's'
        if channel is None:
            sheet.append(['累计通过', sum(state['counts'])])
        else:
            sheet.append([f'通道 {channel:02d} 越线次数', state['counts'][channel - 1]])
        sheet.append(['通道', '数量'])
        for i, count in enumerate(state['counts'], 1):
            if channel is None or i == channel:
                sheet.append([i, count])
        if 'events' in state:
            detail = book.create_sheet('越线明细')
            detail.append(['记录号', '通道', '越线帧（从 1 开始）', '视频时间（秒）', '轨迹号',
                           '前次 X（px）', '前次 Y（px）', '越线 X（px）', '越线 Y（px）', '检测线 Y（px）', '前次观测帧（从 1 开始）', '越线判据'])
            for event in state['events']:
                if channel is not None and event['channel'] != channel:
                    continue
                detail.append([event[key] for key in ('id', 'channel', 'frame', 'time_s', 'track',
                                                      'prev_x', 'prev_y', 'x', 'y', 'line_y')]
                              + [event.get('prev_frame', event['frame'] - 1),
                                 {'temporal_centroid': '局部帧差质心', 'lower_body': '虫体下缘'}.get(
                                     event.get('marker'), '质心')])
            detail.freeze_panes = 'A2'
            detail.auto_filter.ref = detail.dimensions
            for col in 'ABCDEFGHIJKL':
                detail.column_dimensions[col].width = 18 if col == 'D' else 15
        out = io.BytesIO()
        book.save(out)
        return out.getvalue()

    def close(self):
        with self.condition:
            self.closed = True
            threads = [job['thread'] for job in self.jobs.values()]
            self.condition.notify_all()
        for thread in threads:
            thread.join()
        with self.reader_lock:
            self.frame_reader.close()
