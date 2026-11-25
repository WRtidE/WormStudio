"""Local persistent file library. Media URLs expose only registered file IDs."""
import json
import mimetypes
from pathlib import Path
import re
import secrets
import time
from collections import defaultdict
from contextlib import nullcontext
from itertools import chain
from input_validation import reserved_filename
from resource_paths import EXAMPLE_VIDEOS

VIDEO = {'.mp4', '.avi', '.mov', '.mkv', '.m4v', '.mpg', '.mpeg', '.wmv', '.webm'}
IMAGE = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff'}
ALLOWED = VIDEO | IMAGE | {'.csv', '.json', '.txt'}


def load_index(root):
    path = root/'config/workspace.local.json'
    if not path.exists(): return dict(version=2, files=[], folders=[], selected=None), False
    # Fail visibly instead of replacing a damaged index with an empty library.
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or not isinstance(value.get('files'), list): raise ValueError('文件工作区索引无效')
    migrated = value.get('version', 1) < 2
    if migrated:
        value['folders']=[]
        for item in value['files']:
            parts=str(item.get('relativePath','')).replace('\\','/').split('/')[:-1]
            parent=None
            try:
                for part in parts: folder_name(part)
            except ValueError: parts=[]
            for part in parts: parent=ensure_folder(value,part,parent)['id']
            item.update(parent=parent,order=next_order(value,parent))
        value['version']=2
    if not isinstance(value.get('folders'),list): raise ValueError('文件夹索引无效')
    return value, migrated


def read(root):
    value, migrated = load_index(root)
    if migrated:
        write(root, value)
    return value


def write(root, value):
    path = root/'config/workspace.local.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.workspace-'+secrets.token_hex(8)+'.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally: temporary.unlink(missing_ok=True)


def public(item):
    item = dict(item)
    item['available'] = Path(item['path']).is_file()
    item['url'] = '/api/library-media?id='+item['id']+'&inline=1'
    return item


def register(root, path, *, auto=False, **metadata):
    state = read(root)
    path = checked_file(path)
    existing = next((f for f in state['files'] if Path(f['path']) == path), None)
    item, changed = register_item(state, path, existing, auto, metadata)
    if changed:
        write(root, state)
    return public(item) if item is not None else None


def checked_file(path):
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() not in ALLOWED:
        raise ValueError('文件不存在或格式不受支持')
    return path


def register_item(state, path, item, auto, metadata):
    if item is None:
        parent = metadata.get('parent')
        check_parent(state, parent)
        item = dict(id=secrets.token_hex(16), path=str(path), name=path.name,
                    size=path.stat().st_size, kind='video' if path.suffix.lower() in VIDEO else 'image' if path.suffix.lower() in IMAGE else 'file',
                    added=time.time(), parent=parent, order=next_order(state,parent))
        item.update(metadata)
        state['files'].append(item)
        return item, True
    elif item.get('removed'):
        if auto: return None, False
        item.update(removed=False,parent=None,order=next_order(state,None))
        return item, True
    return item, False


def register_many(root, paths, *, auto=True, index=None):
    state, changed = load_index(root) if index is None else index
    by_path = {Path(item['path']): item for item in state['files']}
    files = {}
    for source in dict.fromkeys(paths):
        try:
            path = checked_file(source)
            item, added = register_item(state, path, by_path.get(path), auto, {})
            if item is not None:
                by_path[path] = item
            files[source] = public(item) if item is not None else None
            changed |= added
        except (ValueError, OSError):
            continue  # A moved source must not hide the corresponding saved result.
    if changed:
        write(root, state)
    return files


def listing(root):
    state, changed = load_index(root)
    files_by_path = {Path(item['path']): item for item in state['files']}
    first_layout = 'exampleFolder' not in state
    folder = next((f for f in state['folders'] if f['id'] == state.get('exampleFolder')), None)
    examples = {}
    for mode, name in EXAMPLE_VIDEOS.items():
        path = (root/'examples'/name).resolve()
        item = files_by_path.get(path)
        if not path.is_file() or (item is not None and item.get('removed')):
            continue
        if item is None or (first_layout and item.get('parent') is None):
            if folder is None:
                folder = ensure_folder(state, '示例视频', None)
            parent = folder['id']
            if item is None:
                item, _ = register_item(state, path, None, True, {'parent': parent})
            else:
                # Older workspaces placed bundled clips at the root. Keep their IDs and history.
                item.update(parent=parent, order=next_order(state, parent))
            changed = True
        examples[mode] = item['id']
    if changed or (first_layout and examples):
        if examples:
            state['exampleFolder'] = folder['id'] if folder else None
        write(root, state)
    return dict(files=[public(f) for f in state['files'] if not f.get('removed')],
                folders=state['folders'], selected=state.get('selected'), examples=examples)


def select(root, file_id):
    state = read(root)
    if file_id is not None and not any(f['id'] == file_id and not f.get('removed') for f in state['files']): raise ValueError('文件不在工作区中')
    state['selected'] = file_id
    write(root, state)
    return dict(saved=True)


def media(root, file_id):
    item = next((f for f in load_index(root)[0]['files'] if f['id'] == file_id), None)
    if item is None: return None
    return Path(item['path'])


def receive_file(path, stream, size):
    with path.open('wb') as handle:
        remaining = size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError('传输中断，文件尚未保存，请重新导入')
            handle.write(chunk)
            remaining -= len(chunk)


def upload(root, stream, size, name, relative_path='', signature='', parent=None, *, transfer=nullcontext):
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name.replace('\\', '/').rsplit('/', 1)[-1]).strip('. ')
    if not name or Path(name).suffix.lower() not in ALLOWED: raise ValueError('请选择视频、图片、CSV、JSON 或 TXT')
    if reserved_filename(name): name = 'file-'+name
    if not 0 < size <= 8*1024**3: raise ValueError('文件为空或超过 8 GB；大视频请使用完整路径打开')
    # Check the index before receiving bytes; a corrupt index must not be lost.
    check_parent(read(root), parent)
    folder = root/'input_videos/library'/secrets.token_hex(16)
    folder.mkdir(parents=True)
    temporary, target = folder/'upload.part', folder/name
    try:
        with transfer():
            receive_file(temporary, stream, size)
        temporary.replace(target)
        return register(root, target, relativePath=relative_path[:4096], signature=signature[:4096], cached=True,
                        parent=parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        folder.rmdir()
        raise


def content_type(path):
    return mimetypes.guess_type(path.name)[0] or 'application/octet-stream'


def folder_name(name):
    if not isinstance(name,str) or not name.strip() or name!=name.strip() or len(name)>120 or name in ('.','..') or re.search(r'[\\/:*?"<>|\x00-\x1f]',name):
        raise ValueError('名称须为 1–120 个字符，不能包含路径分隔符或特殊字符')
    return name


def nodes(state,parent):
    return sorted((item for item in chain(state['folders'],state['files']) if not item.get('removed') and item.get('parent')==parent),
                  key=lambda item:(item.get('order',0),item['id']))


def next_order(state,parent):
    return max((item.get('order',0) for item in chain(state['folders'],state['files'])
                if not item.get('removed') and item.get('parent')==parent),default=-1)+1


def check_parent(state,parent):
    if parent is not None and not any(item['id']==parent for item in state['folders']):
        raise ValueError('目标文件夹不存在，请刷新文件树')


def ensure_folder(state,name,parent):
    folder_name(name);check_parent(state,parent)
    existing=next((item for item in state['folders'] if item['parent']==parent and item['name'].casefold()==name.casefold()),None)
    if existing:
        existing['collapsed']=False
        return existing
    if len(state['folders'])>=4096: raise ValueError('工作区文件夹过多')
    siblings=nodes(state,parent)
    folder_ids={f['id'] for f in state['folders']}
    # Insert new folders before loose files, preserving the manual order of
    # all existing siblings instead of re-sorting them on every refresh.
    index=next((i for i,f in enumerate(siblings) if f['id'] not in folder_ids),len(siblings))
    item=dict(id=secrets.token_hex(16),name=name,parent=parent,order=index,collapsed=False)
    siblings.insert(index,item)
    for i,sibling in enumerate(siblings):sibling['order']=i
    state['folders'].append(item)
    return item


def descendants(state,folder_id):
    children = defaultdict(list)
    for folder in state['folders']:
        children[folder.get('parent')].append(folder['id'])
    result={folder_id}
    pending = [folder_id]
    while pending:
        for child in children[pending.pop()]:
            if child not in result:
                result.add(child)
                pending.append(child)
    return result


def organize(root,body):
    """Organize references only. Never delete or rename source media or analysis outputs."""
    if not isinstance(body,dict):raise ValueError('文件树请求无效')
    state=read(root);operation=body.get('op');item_id=body.get('id');parent=body.get('parent')
    item=next((f for f in state['folders']+state['files'] if f['id']==item_id and not f.get('removed')),None)
    items=[item] if item is not None else []
    if operation in ('move','remove') and 'ids' in body:
        ids=body['ids']
        if (not isinstance(ids,list) or not ids or item_id is not None or
                not all(isinstance(key,str) for key in ids) or len(ids)!=len(set(ids))):
            raise ValueError('所选文件无效，请重新选择')
        available={f['id']:f for f in state['files'] if not f.get('removed')}
        if any(key not in available for key in ids):raise ValueError('所选文件已改变，请刷新文件树')
        items=[available[key] for key in ids]
    result={}
    if operation in ('mkdir','ensure-path'):
        check_parent(state,parent)
        if operation=='mkdir':
            name=folder_name(body.get('name'))
            if any(f['parent']==parent and f['name'].casefold()==name.casefold() for f in state['folders']):raise ValueError('此位置已存在同名文件夹')
            result['folder']=ensure_folder(state,name,parent)
            if parent:next(f for f in state['folders'] if f['id']==parent)['collapsed']=False
        else:
            parts=body.get('parts')
            if not isinstance(parts,list) or not 1<=len(parts)<=64:raise ValueError('文件夹路径层级无效')
            for part in parts:folder_name(part)
            for part in parts:parent=ensure_folder(state,part,parent)['id']
            result['folder']=next(f for f in state['folders'] if f['id']==parent)
    elif not items:raise ValueError('文件或文件夹已移除，请刷新文件树')
    elif operation=='rename':
        name=folder_name(body.get('name'))
        if any(f['id']!=item_id and f['parent']==item.get('parent') and f['name'].casefold()==name.casefold() for f in state['folders']):raise ValueError('此位置已存在同名文件夹')
        item['name']=name
    elif operation=='collapse':
        if item not in state['folders'] or type(body.get('collapsed')) is not bool:raise ValueError('折叠请求无效')
        item['collapsed']=body['collapsed']
    elif operation=='move':
        check_parent(state,parent)
        if item in state['folders']:
            if parent in descendants(state,item_id):raise ValueError('不能将文件夹移到自己或子文件夹内')
            if any(f['id']!=item_id and f['parent']==parent and f['name'].casefold()==item['name'].casefold() for f in state['folders']):raise ValueError('目标位置已存在同名文件夹')
        moving_ids={f['id'] for f in items}
        ordered=[f for f in nodes(state,parent) if f['id'] not in moving_ids]
        before=body.get('before')
        if before in moving_ids:return dict(listing(root),**result)
        index=next((i for i,f in enumerate(ordered) if f['id']==before),None) if before else len(ordered)
        if index is None:raise ValueError('排序位置已改变，请刷新后重试')
        for f in items:f['parent']=parent
        ordered[index:index]=items
        for i,f in enumerate(ordered):f['order']=i
        if parent:next(f for f in state['folders'] if f['id']==parent)['collapsed']=False
    elif operation=='remove':
        ids=descendants(state,item_id) if item in state['folders'] else {f['id'] for f in items}
        removed=[f for f in state['files'] if f['id'] in ids or f.get('parent') in ids]
        for f in removed:f.update(removed=True,parent=None)
        state['folders']=[f for f in state['folders'] if f['id'] not in ids]
        if state.get('selected') in {f['id'] for f in removed}:state['selected']=None
    else:raise ValueError('未知文件树操作')
    write(root,state)
    return dict(listing(root),**result)
