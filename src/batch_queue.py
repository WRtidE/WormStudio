"""Persistent, sequential queue of independently saved annotations."""
import copy
import json
from pathlib import Path
import secrets


class BatchQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.items = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []
        self.running = False
        self.active = None
        self.selected_samples = None
        self.results_path = self.path.with_name(self.path.stem + '.results.json')
        self.result_samples = []
        if self.results_path.exists():
            saved = json.loads(self.results_path.read_text(encoding='utf-8'))
            if isinstance(saved, list) and all(isinstance(sample, str) for sample in saved):
                self.result_samples = saved
        for item in self.items:
            if item['status'] == 'running':
                item.update(status='pending', error='上次处理被中断，等待重新启动')

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def add(self, config):
        if self.running:
            raise ValueError('批处理正在运行')
        if any(i['config']['sample'] == config['sample'] for i in self.items):
            raise ValueError('此标注已在队列中')
        self.items.append(dict(id=secrets.token_hex(8), config=copy.deepcopy(config), status='pending', error=''))
        try:
            self.persist()
        except Exception:
            self.items.pop()
            raise

    def snapshot(self):
        def summary(item):
            return dict(id=item['id'], sample=item.get('result_sample', item['config']['sample']),
                        name=Path(item['config']['video']).name, status=item['status'], error=item['error'])
        by_sample = {item['config']['sample']: item for item in self.items}
        return dict(running=self.running, items=[summary(item) for item in self.items],
                    result_items=[summary(by_sample[sample]) for sample in self.result_samples
                                  if sample in by_sample])

    def next_pending(self):
        return next((item for item in self.items if item['status'] == 'pending' and
                     (self.selected_samples is None or item['config']['sample'] in self.selected_samples)), None)

    def start(self, samples=None):
        if self.running:
            raise ValueError('批处理已启动')
        selection = list(dict.fromkeys(samples)) if samples is not None else None
        pending = [item['config']['sample'] for item in self.items if item['status'] == 'pending'
                   and (selection is None or item['config']['sample'] in selection)]
        if not pending:
            raise ValueError('没有待处理任务，或批处理已启动')
        # Explicit sequences define a new viewing group. Resuming or retrying
        # only that group's pending items keeps its completed neighbours.
        group = (selection if selection is not None else self.result_samples
                 if self.result_samples and set(pending).issubset(self.result_samples) else pending)
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.results_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(group, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.results_path)
        self.result_samples = list(group)
        self.selected_samples = set(selection) if selection is not None else None
        self.running = True

    def take(self):
        if not self.running or self.active:
            return None
        item = self.next_pending()
        if item is None:
            self.running = False
            return None
        item.update(status='running', error='')
        self.active = item
        self.persist()
        return dict(copy.deepcopy(item['config']), _resume_saved=True)

    def finish(self, status, error='', cancelled=False, result_sample=None):
        if self.active:
            self.active.update(status=status if status in ('complete', 'failed', 'cancelled') else 'failed', error=error or '')
            if result_sample and status == 'complete': self.active['result_sample'] = result_sample
            self.active = None
            self.persist()
        if cancelled or self.next_pending() is None:
            self.running = False

    def remove(self, identifier):
        if self.running:
            raise ValueError('请先停止批处理')
        self.items = [i for i in self.items if i['id'] != identifier]
        self.persist()

    def retry(self, identifier):
        if self.running:
            raise ValueError('请先停止批处理')
        item = next((i for i in self.items if i['id'] == identifier), None)
        if item is None: raise ValueError('队列任务不存在')
        if item['status'] not in ('failed', 'cancelled'):
            raise ValueError('只有失败或中断的任务可重试')
        item.update(status='pending', error='')
        self.persist()
