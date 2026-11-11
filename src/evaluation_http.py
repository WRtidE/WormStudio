"""Small HTTP adapter for the existing local workspace server."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from evaluation_annotations import DatasetStore, RevisionConflict
from resource_paths import PROJECT_ROOT


def store(server):
    if getattr(server, 'evaluation_store', None) is None:
        setting = PROJECT_ROOT/'config/evaluation_dataset.json'
        configured = os.environ.get('WORMSTUDIO_EVALUATION_DATASET')
        if not configured and setting.is_file():
            configured = json.loads(setting.read_text(encoding='utf-8'))['dataset']
        if not configured: raise ValueError('尚未配置评测任务。请先建立数据盘点、划分和任务清单。')
        path = Path(configured)
        try:
            server.evaluation_store = DatasetStore(path if path.is_absolute() else PROJECT_ROOT/path)
        except FileNotFoundError as exc:
            raise ValueError('找不到已配置的评测数据集。请连接原数据盘，或更新 config/evaluation_dataset.json 中的数据集位置。日常视频分析不受影响。') from exc
    return server.evaluation_store


def get(handler):
    route = urlsplit(handler.path); query = parse_qs(route.query)
    try:
        dataset = store(handler.server)
        if route.path == '/api/evaluation/catalog':
            handler.respond(200, dataset.catalog())
        elif route.path == '/api/evaluation/task':
            handler.respond(200, dataset.load(query.get('task',[''])[0], query.get('role',[''])[0], query.get('annotator',[''])[0]))
        elif route.path == '/api/evaluation/frame':
            content = dataset.frame_image(query.get('task',[''])[0], int(query.get('frame',['-1'])[0]))
            handler.respond(200, content, 'image/jpeg')
        else:
            handler.respond(404, dict(error='未知评测接口'))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        handler.respond(409, dict(error=str(exc)))


def save(handler, progress):
    if not progress or handler.headers.get('X-Annotation-Token') != progress.token:
        handler.respond(403, dict(error='Invalid evaluation session')); return
    try:
        size = int(handler.headers.get('Content-Length','0'))
        if not 0 < size <= 16*1024*1024: raise ValueError('标注请求为空或超过 16 MiB')
        body = json.loads(handler.rfile.read(size))
        if not isinstance(body,dict): raise ValueError('标注请求必须为对象')
        handler.respond(200, store(handler.server).save(body))
    except RevisionConflict as exc:
        handler.respond(409, dict(error=str(exc)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        handler.respond(400, dict(error=str(exc)))
