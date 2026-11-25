"""Persistent, sequential queue of independently saved annotations."""
import copy
import json
from pathlib import Path
import secrets
from datetime import datetime


class BatchQueue:
    def __init__(self, path):
        self.revision = 0
        self.path = Path(path)
        self.items = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []
        self.running = False
        self.active = None
        self.dispatch_error = ''
        self.selected_samples = None
        self.results_path = self.path.with_name(self.path.stem + '.results.json')
        self.result_samples = []
        self.groups = []
        self.archived = {}
        if self.results_path.exists():
            saved = json.loads(self.results_path.read_text(encoding='utf-8'))
            if isinstance(saved, list) and all(isinstance(sample, str) for sample in saved):
                self.result_samples = saved
                if saved:
                    self.groups = [dict(id='legacy', created='', samples=saved)]
            elif isinstance(saved, dict):
                self.result_samples = saved['current']
                self.groups = saved['groups']
                self.archived = saved.get('archived', {})
        for item in self.items:
            if item['status'] == 'running':
                item.update(status='pending', error='上次处理被中断，等待重新启动')

    def persist(self, items=None):
        self.revision += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.items if items is None else items, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def add(self, config):
        self.add_many([config])

    def add_many(self, configs, start_samples=None):
        if self.running:
            raise ValueError('批处理正在运行')
        existing = {item['config']['sample'] for item in self.items}
        additions = []
        for config in configs:
            if config['sample'] in existing:
                raise ValueError('此标注已在队列中')
            existing.add(config['sample'])
            additions.append(dict(id=secrets.token_hex(8), config=copy.deepcopy(config),
                                  status='pending', error=''))
        previous = self.items
        self.items = previous + additions
        try:
            self.persist()
            if start_samples is not None:
                self.start(samples=start_samples)
        except Exception:
            self.items = previous
            if start_samples is not None:
                self.persist()
            raise

    @staticmethod
    def summary(item):
        config = item['config']
        video_name = Path(config['video']).name
        name = video_name + (f" · 槽 {config['chamber_id']}" if 'chamber_id' in config else '')
        return dict(id=item['id'], sample=item.get('result_sample', config['sample']),
                    name=name, status=item['status'], error=item['error'],
                    chamber_id=config.get('chamber_id'), video_name=video_name,
                    video_path=config['video'])

    def group_items(self, key=None):
        samples = self.result_samples
        if key:
            group = next((group for group in self.groups if group['id'] == key), None)
            if not group:
                raise ValueError('找不到这个批次')
            samples = group['samples']
        by_sample = {item['config']['sample']: item for item in self.items}
        return [self.summary(by_sample[sample]) if sample in by_sample else self.archived[sample]
                for sample in samples if sample in by_sample or sample in self.archived]

    def snapshot(self):
        items = [self.summary(item) for item in self.items]
        by_sample = self.archived.copy()
        by_sample.update((item['config']['sample'], summary) for item, summary in zip(self.items, items))
        history = []
        current = None
        for group in reversed(self.groups):
            members = [by_sample[s] for s in group['samples'] if s in by_sample]
            if group['samples'] == self.result_samples:
                current = group['id']
            if members:
                videos = {item['video_path'] for item in members}
                history.append(dict(id=group['id'], created=group['created'], name=Path(members[0]['video_path']).name,
                                    videos=len(videos), total=len(members),
                                    complete=sum(i['status'] == 'complete' for i in members)))
        return dict(running=self.running, dispatch_error=self.dispatch_error, items=items,
                    result_items=[by_sample[sample] for sample in self.result_samples if sample in by_sample],
                    group_id=current, groups=history)

    def next_pending(self):
        return next((item for item in self.items if item['status'] == 'pending' and
                     (self.selected_samples is None or item['config']['sample'] in self.selected_samples)), None)

    def start(self, samples=None, retry_group=None):
        if self.running:
            raise ValueError('批处理已启动')
        selection = list(dict.fromkeys(samples)) if samples is not None else None
        selected = set(selection) if selection is not None else None
        pending = [item['config']['sample'] for item in self.items if item['status'] == 'pending'
                   and (selected is None or item['config']['sample'] in selected)]
        if not pending:
            raise ValueError('没有待处理任务，或批处理已启动')
        # Explicit sequences define a new viewing group. Resuming or retrying
        # only that group's pending items keeps its completed neighbours.
        group = (selection if selection is not None else self.result_samples
                 if self.result_samples and set(pending).issubset(self.result_samples) else pending)
        if retry_group:
            group = next(g['samples'] for g in self.groups if g['id'] == retry_group)
        groups = self.groups.copy()
        if not any(g['samples'] == group for g in groups):
            groups.append(dict(id=secrets.token_hex(8), created=datetime.now().astimezone().isoformat(timespec='minutes'), samples=list(group)))
        self.persist_groups(group, groups)
        self.result_samples = list(group)
        self.groups = groups
        self.selected_samples = selected
        self.dispatch_error = ''
        self.running = True

    def persist_groups(self, group, groups):
        self.revision += 1
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.results_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(current=group, groups=groups, archived=self.archived), ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.results_path)

    def take(self):
        if not self.running or self.active:
            return None
        item = self.next_pending()
        if item is None:
            self.running = False
            return None
        started = dict(item, status='running', error='')
        try:
            self.persist([started if entry is item else entry for entry in self.items])
        except OSError as error:
            self.running = False
            self.dispatch_error = f"{self.summary(item)['name']} 仍待处理，无法保存队列状态：{error}"
            return None
        item.update(started)
        self.active = item
        config = copy.deepcopy(item['config'])
        config['_resume_saved'] = True
        return config

    def retry_dispatch(self):
        if not self.dispatch_error:
            raise ValueError('没有等待重试的队列写入')
        self.dispatch_error = ''
        self.running = True

    def finish(self, status, error='', cancelled=False, result_sample=None, previous_sample=None):
        item = self.active
        if item is None and status == 'complete' and result_sample and previous_sample:
            # A successful correction replaces only the version used by this slot.
            item = next((i for i in self.items if i['config'].get('chamber_id') is not None
                         and i.get('result_sample', i['config']['sample']) == previous_sample), None)
            if item is None:
                for key, saved in self.archived.items():
                    if saved.get('chamber_id') is not None and saved['sample'] == previous_sample:
                        self.archived[key] = dict(saved, sample=result_sample, status='complete', error='')
                        self.persist_groups(self.result_samples, self.groups)
                        break
        if item is None and result_sample:
            # A run resumed outside the queue (open/continue from history) keeps
            # its original record; adopt it so the finished status is not lost.
            item = next((i for i in self.items
                         if i.get('result_sample') == result_sample
                         or i['config'].get('sample') == result_sample), None)
            if item is not None and item['status'] not in ('failed', 'cancelled', 'pending'):
                item = None
        if item is not None:
            if self.dispatch_error and item is self.next_pending():
                self.dispatch_error = ''
            item.update(status=status if status in ('complete', 'failed', 'cancelled') else 'failed', error=error or '')
            if result_sample and status == 'complete': item['result_sample'] = result_sample
            if item is self.active: self.active = None
            self.persist()
        if cancelled or self.next_pending() is None:
            self.running = False

    def group_for_result(self, sample):
        keys = {i['config']['sample'] for i in self.items
                if i.get('result_sample', i['config']['sample']) == sample}
        keys.update(k for k, i in self.archived.items() if i['sample'] == sample)
        return next((g['id'] for g in reversed(self.groups) if keys.intersection(g['samples'])), None)

    def remove(self, identifier):
        if self.running:
            raise ValueError('请先停止批处理')
        item = next((i for i in self.items if i['id'] == identifier), None)
        dispatch_item = self.next_pending() if self.dispatch_error else None
        if item and any(item['config']['sample'] in g['samples'] for g in self.groups):
            summary = dict(self.summary(item), removed=True)
            if summary['status'] == 'pending':
                summary.update(status='removed', error='已从待处理队列移出')
            self.archived[item['config']['sample']] = summary
            self.persist_groups(self.result_samples, self.groups)
        self.items = [i for i in self.items if i['id'] != identifier]
        self.persist()
        if dispatch_item is not None and item is dispatch_item:
            self.dispatch_error = ''

    def retry(self, identifier, start=False, group=None):
        if self.running:
            raise ValueError('请先停止批处理')
        item = next((i for i in self.items if i['id'] == identifier), None)
        if item is None: raise ValueError('队列任务不存在')
        if item['status'] not in ('failed', 'cancelled'):
            raise ValueError('只有失败或中断的任务可重试')
        sample = item['config']['sample']
        if group and not any(g['id'] == group and sample in g['samples'] for g in self.groups):
            raise ValueError('任务不属于所选批次')
        previous = item.copy()
        item.update(status='pending', error='')
        self.persist()
        if start:
            group = group or next((g['id'] for g in reversed(self.groups) if sample in g['samples']), None)
            try:
                self.start(samples=[sample], retry_group=group)
            except Exception:
                item.update(previous)
                self.persist()
                raise
