"""Independent human annotations and append-only review revisions.

The original pipeline annotations and predictions are not ground truth inputs.
This store serves source images only and never writes into experiment outputs.
"""
import math
from pathlib import Path
import re
import secrets

from evaluation_dataset import VERSION, digest, now, read_json, sha256, write_new

ANNOTATION_VERSION = 'wormstudio-human-annotation-v1'
ROLES = ('annotator_a', 'annotator_b', 'adjudicator')


class RevisionConflict(ValueError):
    pass


def choice(value, values, label):
    if value not in values:
        raise ValueError(label + ' 无效')


def evidence(value):
    return isinstance(value, str) and bool(value.strip())


def point(value, width, height):
    if not isinstance(value, list) or len(value) != 2 or any(type(n) not in (int, float) or not math.isfinite(n) for n in value):
        raise ValueError('坐标必须为两个有限数值')
    if not (0 <= value[0] < width and 0 <= value[1] < height):
        raise ValueError('坐标超出原图范围')


def paths(value, minimum, width, height):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('路径必须为数组，最多 100 段')
    for line in value:
        if not isinstance(line, list) or not minimum <= len(line) <= 10000:
            raise ValueError('路径点数无效')
        for p in line: point(p, width, height)
        if len({tuple(p) for p in line}) < minimum: raise ValueError('路径存在不足数量的不同点')
        if minimum == 3:
            area = abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(line, line[1:]+line[:1]))) / 2
            if area <= 0: raise ValueError('轮廓或未知区域面积为零')


def validate_frames(frames, task, video, submitted=False):
    if not isinstance(frames, list) or not frames: raise ValueError('至少保存一帧标注')
    seen = set(); width = video['metadata']['width']; height = video['metadata']['height']
    for frame in frames:
        if not isinstance(frame, dict): raise ValueError('每帧标注必须为对象')
        n = frame.get('frame')
        if type(n) is not int or n not in task['frames'] or n in seen: raise ValueError('标注帧重复或不属于当前任务')
        seen.add(n)
        choice(frame.get('source_status'), ('usable','corrupt','unknown'), '源帧状态')
        if type(frame.get('complete')) is not bool: raise ValueError('需明确本帧是否完成')
        if submitted and not frame['complete']: raise ValueError('提交前需逐帧确认完成；不可见处请明确标为未知')
        paths(frame.get('unknown_regions', []), 3, width, height)
        worms = frame.get('objects')
        if not isinstance(worms, list) or len(worms) > 100: raise ValueError('个体列表无效')
        ids = set()
        for obj in worms:
            if not isinstance(obj, dict): raise ValueError('个体标注必须为对象')
            key = obj.get('local_id')
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',key) or key in ids: raise ValueError('本帧个体编号无效或重复')
            ids.add(key)
            mask = obj.get('mask', {}); center = obj.get('centerline', {})
            choice(mask.get('status'), ('visible','partial','unknown','absent'), '掩膜状态')
            paths(mask.get('polygons', []), 3, width, height); paths(mask.get('holes', []), 3, width, height)
            if mask['status'] in ('visible','partial') and not mask.get('polygons'): raise ValueError('可见掩膜必须有轮廓')
            if mask['status'] in ('unknown','absent') and (mask.get('polygons') or mask.get('holes')): raise ValueError('未知／缺失掩膜不能同时含测量轮廓')
            choice(center.get('status'), ('visible','partial','unknown','absent'), '中线状态')
            choice(center.get('direction'), ('unoriented','head_to_tail','unknown'), '中线方向')
            paths(center.get('segments', []), 2, width, height)
            if center['status'] in ('visible','partial') and not center.get('segments'): raise ValueError('可见中线必须有路径')
            if center['status'] in ('unknown','absent') and center.get('segments'): raise ValueError('未知／缺失中线不能同时含测量路径')
            endpoints = obj.get('endpoints', {})
            for name in ('a','b'):
                endpoint = endpoints.get(name, {})
                choice(endpoint.get('visibility'), ('visible','occluded','out_of_frame','unknown'), '端点可见性')
                if endpoint['visibility'] == 'visible': point(endpoint.get('point'), width, height)
                elif endpoint.get('point') is not None: raise ValueError('不可见端点不能伪造位置')
            identity = obj.get('identity', {})
            choice(identity.get('status'), ('unknown','ambiguous','confirmed'), '身份状态')
            if identity['status'] == 'confirmed':
                if not evidence(identity.get('individual_id')) or not evidence(identity.get('evidence')):
                    raise ValueError('确认身份需要个体编号及独立证据')
            elif identity.get('individual_id') is not None: raise ValueError('未确认身份请保留 individual_id=null')
            head = obj.get('head_tail', {})
            choice(head.get('status'), ('unknown','confirmed'), '头尾状态')
            if head['status'] == 'confirmed':
                if head.get('head_endpoint') not in ('a','b') or not evidence(head.get('evidence')): raise ValueError('头尾确认需要端点及证据')
                if endpoints[head['head_endpoint']]['visibility'] != 'visible': raise ValueError('头端确认必须有可见端点')
            elif head.get('head_endpoint') is not None: raise ValueError('头尾未知时不能指定头端')
            if center['direction'] == 'head_to_tail' and head['status'] != 'confirmed': raise ValueError('头尾未知时只允许无方向中线')
            crossing = obj.get('self_crossing', {})
            choice(crossing.get('status'), ('none','present','unknown'), '自交状态')
            paths(crossing.get('connections', []), 2, width, height)
            if crossing.get('connections') and crossing['status'] != 'present': raise ValueError('自交连接需标明存在自交')
            if crossing.get('connections') and not evidence(crossing.get('evidence')): raise ValueError('自交连接需给出观察依据')
            if frame['source_status'] != 'usable' and (mask.get('polygons') or center.get('segments') or crossing.get('connections') or any(p.get('point') for p in endpoints.values()) or identity['status']=='confirmed' or head['status']=='confirmed'):
                raise ValueError('损坏／未知源帧只能记录未知，不能提交几何测量')
    if submitted and seen != set(task['frames']): raise ValueError('提交必须覆盖当前任务全部帧')


def empty_frame(frame):
    return dict(frame=frame, source_status='unknown', complete=False, unknown_regions=[], objects=[], notes='')


class DatasetStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = read_json(self.root/'manifest.json')
        self.splits = read_json(self.root/'splits.json')
        self.tasks_file = read_json(self.root/'tasks.json')
        if self.manifest.get('schema_version') != VERSION or self.splits.get('manifest_sha256') != digest(self.manifest) or self.tasks_file.get('manifest_sha256') != digest(self.manifest) or self.tasks_file.get('split_sha256') != digest(self.splits):
            raise ValueError('评测清单／划分／任务签名不一致，请使用同一冻结版本')
        self.videos = {v['video_id']:v for v in self.manifest['videos']}
        self.tasks = {t['task_id']:t for t in self.tasks_file['tasks']}
        if len(self.tasks) != len(self.tasks_file['tasks']): raise ValueError('任务编号重复')
        if len(self.videos) != len(self.manifest['videos']): raise ValueError('视频编号重复')
        assignments = {r['video_id']:r for r in self.splits['assignments']}
        if set(assignments) != set(self.videos): raise ValueError('划分清单不完整')
        for task in self.tasks.values():
            video = self.videos.get(task['video_id'])
            if not re.fullmatch(r'task-[0-9a-f]{16}', task['task_id']) or not video or task['source_sha256'] != video['sha256']:
                raise ValueError('任务标识或源视频签名不一致')
            if assignments[task['video_id']]['split'] not in ('development','validation'):
                raise ValueError('试点任务不能使用待划分或最终留出视频')
            if not task['frames'] or len(task['frames'])!=len(set(task['frames'])) or any(type(f) is not int or not 0<=f<video['metadata']['frame_count'] for f in task['frames']):
                raise ValueError('任务帧号越界或重复')
        self._checked = {}

    def task(self, task_id):
        if task_id not in self.tasks: raise ValueError('找不到评测任务')
        return self.tasks[task_id]

    def catalog(self):
        return dict(schema_version=VERSION, dataset=self.root.name, counts=self.splits['counts'], readiness=self.splits['readiness'],
            tasks=[dict(task_id=t['task_id'], video_id=t['video_id'], name=Path(self.videos[t['video_id']]['primary_path']).name,
                stratum=t['stratum'], first_frame=t['frames'][0], last_frame=t['frames'][-1], frame_count=len(t['frames']),
                event=t.get('event')) for t in self.tasks.values()])

    def latest(self, task_id, role):
        self.task(task_id); choice(role, ROLES, '标注角色')
        folder = self.root/'annotations'/'human'/task_id/role
        files = sorted(folder.glob('*.json')) if folder.exists() else []
        return read_json(files[-1]) if files else None

    def load(self, task_id, role, annotator=''):
        task = self.task(task_id); choice(role, ROLES, '标注角色')
        row = self.latest(task_id, role)
        if row and row['annotator'] != annotator.strip():
            raise ValueError('该角色已有其他标注者的记录，请使用原署名或另一独立角色')
        data = dict(task=task, video=dict(video_id=task['video_id'], sha256=task['source_sha256'], **self.videos[task['video_id']]['metadata']),
                    annotation=row, comparisons=[])
        if role == 'adjudicator':
            comparisons = [self.latest(task_id, r) for r in ROLES[:2]]
            if not all(r and r['status']=='submitted' for r in comparisons): raise ValueError('两份独立标注均提交后才能裁决')
            data['comparisons'] = comparisons
        return data

    def save(self, body):
        task = self.task(body.get('task_id')); role = body.get('role'); choice(role, ROLES, '标注角色')
        annotator = body.get('annotator', '').strip()
        if not annotator or len(annotator)>120: raise ValueError('请填写真实标注者署名')
        current = self.latest(task['task_id'], role)
        if current and current['annotator'] != annotator: raise ValueError('不能覆盖其他标注者的角色记录')
        if body.get('base_revision') != (current['revision_id'] if current else None): raise RevisionConflict('该标注已有新版本，请重新加载后再保存')
        choice(body.get('status'), ('draft','submitted'), '保存状态')
        other_roles = [r for r in ROLES if r != role]
        if any((r := self.latest(task['task_id'], other)) and r['annotator'] == annotator for other in other_roles):
            raise ValueError('双人独立标注及裁决必须由不同人员完成')
        frames = body.get('frames'); validate_frames(frames, task, self.videos[task['video_id']], body['status']=='submitted')
        review = body.get('review')
        if role == 'adjudicator':
            comparisons = [self.latest(task['task_id'], r) for r in ROLES[:2]]
            if not all(r and r['status']=='submitted' for r in comparisons): raise ValueError('两份独立标注尚未提交')
            if not isinstance(review,dict) or review.get('compared_revision_ids') != [r['revision_id'] for r in comparisons] or not evidence(review.get('decision_notes')):
                raise ValueError('裁决需引用当前两份独立标注并填写分歧处理依据')
        elif review is not None: raise ValueError('独立标注不能写入裁决结论')
        elapsed = body.get('active_seconds', 0)
        if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 30*24*3600: raise ValueError('标注耗时无效')
        sequence = (current['sequence']+1) if current else 1
        revision = f'{sequence:06d}-' + secrets.token_hex(8)
        record = dict(schema_version=ANNOTATION_VERSION, annotation_source='human_manual', assist='none',
            task_id=task['task_id'], video_id=task['video_id'], source_sha256=task['source_sha256'],
            manifest_sha256=digest(self.manifest), tasks_sha256=digest(self.tasks_file),
            role=role, annotator=annotator, status=body['status'], created_at=now(), sequence=sequence,
            revision_id=revision, parent_revision=body.get('base_revision'),
            truth_status='adjudicated' if role=='adjudicator' and body['status']=='submitted' else 'independent_annotation' if body['status']=='submitted' else 'draft',
            coordinate_system=task['coordinate_system'], frames=frames, review=review, active_seconds=elapsed,
            effort_definition='Client active-session seconds; not independently verified labor time')
        write_new(self.root/'annotations'/'human'/task['task_id']/role/(revision+'.json'), record)
        return record

    def frame_image(self, task_id, frame):
        import cv2
        task = self.task(task_id)
        if type(frame) is not int or frame not in task['frames']: raise ValueError('帧不在当前任务范围内')
        row = self.videos[task['video_id']]
        path = next((Path(a['path']) for a in row['aliases'] if Path(a['path']).is_file()), None)
        if path is None: raise ValueError('原视频已移动，请建立新的路径映射与数据快照')
        stat = path.stat(); signature = (str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        if self._checked.get(row['video_id']) != signature:
            if sha256(path) != row['sha256']: raise ValueError('原视频哈希变化，拒绝使用旧标注坐标')
            self._checked[row['video_id']] = signature
        cap = cv2.VideoCapture(str(path))
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES,frame); ok,image = cap.read()
            if not ok: raise ValueError('源帧无法解码，请记录源数据问题')
            if image.shape[:2] != (row['metadata']['height'], row['metadata']['width']): raise ValueError('源帧尺寸与 manifest 不一致')
            ok, data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok: raise ValueError('预览编码失败')
            return data.tobytes()
        finally:
            cap.release()

    def verify(self):
        checked=0
        for path in (self.root/'annotations'/'human').glob('*/*/*.json'):
            record=read_json(path); task=self.task(record['task_id'])
            if record.get('schema_version') != ANNOTATION_VERSION or record['source_sha256']!=task['source_sha256'] or record['manifest_sha256']!=digest(self.manifest) or record['tasks_sha256']!=digest(self.tasks_file):
                raise ValueError('标注来源版本不一致：'+str(path))
            validate_frames(record['frames'],task,self.videos[task['video_id']],record['status']=='submitted')
            checked+=1
        return dict(valid=True,dataset=self.root.name,videos=len(self.videos),tasks=len(self.tasks),annotation_revisions=checked,
            counts=self.splits['counts'],readiness=self.splits['readiness'],source_files_rehashed=False)

    def export_truth(self, output):
        """Only current, submitted third-person decisions qualify as reference truth."""
        self.verify()
        records=[]; excluded=[]
        for task_id in self.tasks:
            row=self.latest(task_id, 'adjudicator')
            independent=[self.latest(task_id,r) for r in ROLES[:2]]
            if not row or row['status']!='submitted':
                excluded.append(dict(task_id=task_id,reason='no_submitted_adjudication')); continue
            if not all(r and r['status']=='submitted' for r in independent) or row['review']['compared_revision_ids'] != [r['revision_id'] for r in independent]:
                excluded.append(dict(task_id=task_id,reason='independent_annotations_changed')); continue
            if len({r['annotator'] for r in [*independent,row]})!=3:
                raise ValueError('独立标注及裁决署名重复')
            records.append(row)
        if not records: raise ValueError('没有已完成且仍有效的裁决；不能把草稿、预测或空标注导出为真值')
        result=dict(schema_version=ANNOTATION_VERSION,created_at=now(),truth_status='adjudicated',
            manifest_sha256=digest(self.manifest),split_sha256=digest(self.splits),tasks_sha256=digest(self.tasks_file),
            records=records,excluded_tasks=excluded,complete=len(records)==len(self.tasks),
            note='Unknown regions and dimensions remain unknown. Random and event strata must be reported separately.')
        write_new(output,result)
        return dict(output=str(output),adjudicated_tasks=len(records),excluded_tasks=len(excluded),complete=result['complete'])
