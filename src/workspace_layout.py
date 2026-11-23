"""Workspace layout preferences, independent of the server's temporary port."""
import json
import math
import secrets
import threading

_lock = threading.RLock()
_fields = {'x', 'y', 'width', 'height', 'collapsed'}


def read(root):
    path = root / 'config' / 'layout.local.json'
    with _lock:
        if not path.exists():
            return dict(version=1, exists=False, panels={})
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('version') != 1:
                raise ValueError('布局版本无法读取')
            panels = validate(data.get('panels'))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError('布局记录无法读取，请备份 config/layout.local.json 后恢复默认布局') from exc
        return dict(version=1, exists=True, panels=panels)


def validate(panels):
    if not isinstance(panels, dict) or len(panels) > 64:
        raise ValueError('布局组件数量无效')
    for key, value in panels.items():
        if (not isinstance(key, str) or len(key) > 96 or
                not key.startswith(('studio:', 'annotation:', 'processing:')) or
                any(c in key for c in '\\/\x00')):
            raise ValueError('布局组件名称无效')
        if not isinstance(value, dict) or not set(value) <= _fields:
            raise ValueError('布局属性无效')
        for name, number in value.items():
            if name == 'collapsed':
                if type(number) is not bool: raise ValueError('折叠状态无效')
            elif type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 100000:
                raise ValueError('布局尺寸或位置无效')
    return panels


def update(root, body):
    if not isinstance(body, dict) or set(body) - {'panels', 'reset'}:
        raise ValueError('布局请求无效')
    if 'reset' in body and type(body['reset']) is not bool:
        raise ValueError('布局重置请求无效')
    changes = validate(body.get('panels', {}))
    with _lock:
        panels = {} if body.get('reset') else read(root)['panels']
        panels.update(changes)
        validate(panels)
        path = root / 'config' / 'layout.local.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name('.layout-' + secrets.token_hex(6) + '.tmp')
        try:
            temporary.write_text(json.dumps(dict(version=1, panels=panels), ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return dict(saved=True)
