"""Durable, ordered batches of independent channel-counting runs."""
import json
import re
import uuid
from datetime import datetime


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def new_run():
    return datetime.now().strftime('%Y%m%d%H%M%S%f')+'-'+uuid.uuid4().hex[:8]


class ChannelBatch:
    """All transitions run under the counting service's condition lock."""

    def __init__(self, counts, defaults):
        self.counts = counts
        self.defaults = defaults
        self.path = counts.root/'outputs'/'channel-counts'/'batch.json'
        self.record = {'id': None, 'items': []}
        self.running = False
        self.current = None
        self.error = ''
        self.load_error = ''
        if self.path.exists():
            try:
                self.record = self.load()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.load_error = f'无法读取已有批次，请检查 {self.path.name}：{exc}'

    def load(self):
        record = read(self.path)
        if not isinstance(record['items'], list) or not record['items'] or not isinstance(record['id'], str):
            raise ValueError('批次清单格式无效')
        for item in record['items']:
            folder = self.counts.folder(item['file'])
            if not {'name','settings','source_stamp','total','error'} <= item.keys() or item['status'] not in (
                    'running','pending','complete','interrupted','failed','skipped'):
                raise ValueError('视频任务记录无效')
            if not re.fullmatch(r'[0-9]{20}-[a-f0-9]{8}', item['run']):
                raise ValueError('批次中的计数记录编号无效')
            if item['status'] == 'running':
                path = folder/item['run']/'state.json'
                if path.is_file():
                    self.record_result(item, self.counts.state(item['file'], item['run'], False)['task'])
                else:
                    item['status'] = 'pending'
        return record

    @staticmethod
    def record_result(item, state):
        item.update(status=state['status'], error=state.get('error') or state.get('warning') or '',
                    total=sum(state['counts']))

    def snapshot(self):
        with self.counts.condition:
            items = [{k: row[k] for k in ('file','name','run','status','error','total')}
                     for row in self.record['items']]
            if self.current:
                job = self.counts.jobs.get(self.current['file'])
                if job and job['paused']:
                    next(i for i in items if i['run'] == self.current['run'])['status'] = 'paused'
            return dict(id=self.record['id'], running=self.running, items=items, defaults=self.defaults,
                        available=not self.load_error, error=self.load_error or self.error)

    def reserved(self, file):
        return any(i['file'] == file and i['status'] in ('pending','interrupted','running')
                   for i in self.record['items'])

    def action(self, body):
        with self.counts.condition:
            if self.load_error:
                raise ValueError(self.load_error)
            cmd, key = body.get('action'), body.get('id')
            if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9-]{36}', key):
                raise ValueError('批次编号无效')
            rows = self.record['items']
            if cmd == 'start':
                if key == self.record['id']:
                    return self.snapshot()
                if body.get('previous') != self.record['id'] or any(i['status'] in ('pending','interrupted','running') for i in rows):
                    raise ValueError('请先处理当前批次，或取消剩余视频')
                items = self.counts.prepare_batch(body)
                record = dict(id=key, items=items)
                save(self.path, record)
                self.record = record
            elif key != self.record['id']:
                raise ValueError('批次已改变，请查看最新列表')
            elif cmd == 'pause':
                self.running = False
                return self.snapshot()
            elif cmd == 'cancel':
                updated = [dict(row, status='skipped') if row['status'] in ('pending','interrupted') else row for row in rows]
                record = dict(self.record, items=updated)
                save(self.path, record)
                self.record = record
                self.running = False
                return self.snapshot()
            elif cmd == 'retry':
                if self.running or self.current is not None:
                    raise ValueError('请等待当前视频完成')
                updated = [dict(row, status='pending', run=new_run(), total=None, error='') if row['status']=='failed' else row for row in rows]
                record = dict(self.record, items=updated)
                save(self.path, record)
                self.record = record
            elif cmd != 'resume':
                raise ValueError('未知的批次操作')
            self.running = True
            self.error = ''
            try:
                self.dispatch()
            except OSError as exc:
                self.running = False
                self.error = '写入批次进度失败：' + str(exc)
                raise
            return self.snapshot()

    def dispatch(self):
        if not self.running or self.current is not None or self.counts.closed:
            return
        for item in self.record['items']:
            if item['status'] not in ('pending','interrupted'):
                continue
            previous = item['status']
            item['status'] = 'running'
            # Publish the exact run ID before launching. Recovery can find a finished
            # run even if the app exited between its final checkpoint and this manifest.
            try:
                save(self.path, self.record)
            except OSError:
                item['status'] = previous
                raise
            self.current = item
            try:
                self.counts.launch_batch_item(item)
                return
            except (OSError, ValueError, RuntimeError) as exc:
                item.update(status='failed', error=str(exc))
                self.current = None
                save(self.path, self.record)
        self.running = False

    def finished(self, state):
        if self.current is None or self.current['run'] != state['id']:
            return
        self.record_result(self.current, state)
        self.current = None
        try:
            save(self.path, self.record)
            self.dispatch()
        except OSError as exc:
            self.running = False
            self.error = '写入批次进度失败：' + str(exc)
