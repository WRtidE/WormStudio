"""Local persistent file library. Media URLs expose only registered file IDs."""
import json
import mimetypes
from pathlib import Path
import re
import secrets
import time

VIDEO = {'.mp4', '.avi', '.mov', '.mkv', '.m4v', '.mpg', '.mpeg', '.wmv', '.webm'}
IMAGE = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff'}
ALLOWED = VIDEO | IMAGE | {'.csv', '.json', '.txt'}


def read(root):
    path = root/'config/workspace.local.json'
    if not path.exists(): return dict(version=2, files=[], folders=[], selected=None)
    # Fail visibly instead of replacing a damaged index with an empty library.
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or not isinstance(value.get('files'), list): raise ValueError('文件工作区索引无效')
    if value.get('version',1) < 2:
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
        write(root,value)
    if not isinstance(value.get('folders'),list): raise ValueError('文件夹索引无效')
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
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() not in ALLOWED: raise ValueError('文件不存在或格式不受支持')
    state = read(root)
    item = next((f for f in state['files'] if Path(f['path']) == path), None)
    if item is None:
        item = dict(id=secrets.token_hex(16), path=str(path), name=path.name,
                    size=path.stat().st_size, kind='video' if path.suffix.lower() in VIDEO else 'image' if path.suffix.lower() in IMAGE else 'file',
                    added=time.time(), parent=None, order=next_order(state,None))
        item.update(metadata)
        state['files'].append(item)
        write(root, state)
    elif item.get('removed'):
        if auto: return None
        item.update(removed=False,parent=None,order=next_order(state,None))
        write(root,state)
    return public(item)


def listing(root):
    state = read(root)
    return dict(files=[public(f) for f in state['files'] if not f.get('removed')],
                folders=state['folders'], selected=state.get('selected'))


def select(root, file_id):
    state = read(root)
    if file_id is not None and not any(f['id'] == file_id and not f.get('removed') for f in state['files']): raise ValueError('文件不在工作区中')
    state['selected'] = file_id
    write(root, state)
    return dict(saved=True)


def media(root, file_id):
    item = next((f for f in read(root)['files'] if f['id'] == file_id), None)
    if item is None: return None
    return Path(item['path'])


def upload(root, stream, size, name, relative_path='', signature='', parent=None):
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name.replace('\\', '/').rsplit('/', 1)[-1]).strip('. ')
    if not name or Path(name).suffix.lower() not in ALLOWED: raise ValueError('请选择视频、图片、CSV、JSON 或 TXT')
    if name.split('.')[0].lower() in {'con','prn','aux','nul',*[f'{p}{i}' for p in ('com','lpt') for i in range(1,10)]}: name = 'file-'+name
    if not 0 < size <= 8*1024**3: raise ValueError('文件为空或超过 8 GB；大视频请使用完整路径打开')
    # Check the index before receiving bytes; a corrupt index must not be lost.
    state=read(root)
    check_parent(state,parent)
    folder = root/'input_videos/library'/secrets.token_hex(16)
    folder.mkdir(parents=True)
    temporary, target = folder/'upload.part', folder/name
    try:
        with temporary.open('wb') as handle:
            remaining = size
            while remaining:
                chunk = stream.read(min(1024*1024, remaining))
                if not chunk: raise ValueError('传输中断，文件尚未保存，请重新导入')
                handle.write(chunk)
                remaining -= len(chunk)
        temporary.replace(target)
        return register(root, target, relativePath=relative_path[:4096], signature=signature[:4096], cached=True,
                        parent=parent,order=next_order(state,parent))
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
    return sorted((item for item in state.get('folders',[])+state['files'] if not item.get('removed') and item.get('parent')==parent),
                  key=lambda item:(item.get('order',0),item['id']))


def next_order(state,parent):
    return max((item.get('order',0) for item in nodes(state,parent)),default=-1)+1


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
    result={folder_id}
    while True:
        extra={f['id'] for f in state['folders'] if f.get('parent') in result}
        if extra<=result:return result
        result.update(extra)


def organize(root,body,active_video=None):
    """Organize references only. Never delete or rename source media or analysis outputs."""
    if not isinstance(body,dict):raise ValueError('文件树请求无效')
    state=read(root);operation=body.get('op');item_id=body.get('id');parent=body.get('parent')
    item=next((f for f in state['folders']+state['files'] if f['id']==item_id and not f.get('removed')),None)
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
    elif item is None:raise ValueError('文件或文件夹已移除，请刷新文件树')
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
        ordered=[f for f in nodes(state,parent) if f['id']!=item_id]
        before=body.get('before')
        if before==item_id:return dict(listing(root),**result)
        index=next((i for i,f in enumerate(ordered) if f['id']==before),None) if before else len(ordered)
        if index is None:raise ValueError('排序位置已改变，请刷新后重试')
        item['parent']=parent;ordered.insert(index,item)
        for i,f in enumerate(ordered):f['order']=i
        if parent:next(f for f in state['folders'] if f['id']==parent)['collapsed']=False
    elif operation=='remove':
        ids=descendants(state,item_id) if item in state['folders'] else {item_id}
        removed=[f for f in state['files'] if f['id'] in ids or f.get('parent') in ids]
        if active_video and any(Path(f['path']).resolve()==Path(active_video).resolve() for f in removed):
            raise ValueError('此文件正在标注或处理，完成后再从工作区移除')
        for f in removed:f.update(removed=True,parent=None)
        state['folders']=[f for f in state['folders'] if f['id'] not in ids]
        if state.get('selected') in {f['id'] for f in removed}:state['selected']=None
    else:raise ValueError('未知文件树操作')
    write(root,state)
    return dict(listing(root),**result)
