"""Persistent run catalogue and reversible workspace operations."""
import json
from pathlib import Path
import secrets
import time
import hashlib
import copy
import shutil
from resource_paths import motion_constraints_path
from datetime import datetime,timezone


def catalog(root):
    runs=[]
    for config_path in (root/'outputs').glob('*/pipeline-config.json'):
        try:
            config=json.loads(config_path.read_text(encoding='utf-8'))
            state_path=config_path.parent/'pipeline-state.json'
            state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            runs.append(dict(sample=config_path.parent.name, video=config['video'],
                method=config.get('centerline_method','topology'),
                complete=state.get('completed_through')=='export',
                has_metrics=state.get('stages',{}).get('metrics',{}).get('status')=='complete',
                updated=config_path.stat().st_mtime))
        except (OSError,ValueError,KeyError): continue
    targets_path=root/'config/target_videos.json'
    targets=json.loads(targets_path.read_text(encoding='utf-8')) if targets_path.exists() else []
    from workspace_library import read, register
    hidden = set(read(root).get('hidden_history', []))
    for run in runs:
        try: run['file']=register(root,run['video'],auto=True)
        except (ValueError,OSError): pass  # Saved results remain available if a source was moved.
    runs.sort(key=lambda r:r['updated'], reverse=True)
    history = [dict(sample=r['sample'], complete=r['complete'], has_metrics=r['has_metrics'])
               for r in runs if r['sample'] not in hidden][:100]
    return dict(runs=runs[:100], history=history, targets=targets)


def remove_history_entry(root, sample):
    from workspace_library import read, write
    if not isinstance(sample, str) or not sample or Path(sample).name != sample or sample in ('.', '..') or '\\' in sample:
        raise ValueError('无效的任务名称')
    state = read(root)
    hidden = state.setdefault('hidden_history', [])
    if sample in hidden:
        return
    if not (root/'outputs'/sample/'pipeline-config.json').is_file():
        raise ValueError('历史任务不存在，请刷新列表')
    hidden.append(sample)
    write(root, state)


def load_config(root, sample):
    if not isinstance(sample,str) or Path(sample).name!=sample or '/' in sample or '\\' in sample:
        raise ValueError('无效的任务名称')
    base=(root/'outputs'/sample).resolve()
    if not base.is_relative_to((root/'outputs').resolve()): raise ValueError('任务路径超出结果目录')
    config=json.loads((base/'pipeline-config.json').read_text(encoding='utf-8'))
    if config['sample']!=sample: raise ValueError('任务配置名称不匹配')
    return base,config


def confirm_head(root, sample, worm, frame, endpoint):
    """User review creates a derived run; segmentation and pose stay unchanged."""
    import numpy as np
    from centerline_methods import method_source
    from run_storage import fresh_sample, input_identity
    from worm_pipeline import complete, build_stages, validate_config
    old,config=load_config(root,sample)
    if type(worm) is not int or type(frame) is not int or type(endpoint) is not int or endpoint not in (0,1):
        raise ValueError('头部复核需要有效的虫编号、帧号和A/B端点')
    source=method_source(config['centerline_method']);result=old/('phenotypes-'+source)
    rows=json.loads((result/'per_frame_metrics.json').read_text(encoding='utf-8'))
    row=next((r for r in rows if r['worm_id']==worm and r['frame']==frame),None)
    if not row or not row['centerline_valid'] or not row['identity_valid'] or row.get('endpoint_a_inferred') or row.get('endpoint_b_inferred'):
        raise ValueError('该帧没有可人工确认的可靠端点，请选择清晰的有效姿态帧')
    if row.get('centerline_topology','simple')!='simple': raise ValueError('自交帧不用于头尾确认')
    with np.load(result/'centerlines.npz',allow_pickle=False) as lines:
        point=lines[f'w{worm}_f{frame}'][0 if endpoint==0 else -1].tolist()
    from head_tail import normalize_anchors
    anchors=normalize_anchors(config.get('head_annotations',{}).get(str(worm)))
    space = {}
    summary_path = result/'summary.json'
    if summary_path.is_file():
        crop = json.loads(summary_path.read_text(encoding='utf-8-sig')).get('crop_xyxy') or config['crop_xyxy']
        if crop != config['crop_xyxy']:
            point = (np.array(point)+crop[:2]).tolist()
            space = dict(point_space='source')
    anchors.append(dict(id=secrets.token_hex(8),frame=frame,point=point,**space,
        source='interactive_user_endpoint_confirmation',confirmed_at=datetime.now(timezone.utc).isoformat()))
    config.setdefault('head_annotations',{})[str(worm)]=anchors
    return derive_geometry_run(root,sample,config)


def remove_head(root,sample,worm,anchor_id):
    from head_tail import normalize_anchors
    _,config=load_config(root,sample)
    if type(worm) is not int or not isinstance(anchor_id,str):raise ValueError('锚点标识无效')
    anchors=normalize_anchors(config.get('head_annotations',{}).get(str(worm)))
    remaining=[a for a in anchors if a['id']!=anchor_id]
    if len(remaining)==len(anchors):raise ValueError('锚点不存在，请重新加载')
    config.setdefault('head_annotations',{})[str(worm)]=remaining
    return derive_geometry_run(root,sample,config)


def identity_links(root,sample):
    base,config=load_config(root,sample)
    from centerline_methods import method_source
    path=base/('phenotypes-'+method_source(config['centerline_method']))/'identity-links.json'
    if not path.is_file():return dict(version=None,edges=[],notice='旧结果尚未计算片段关联')
    return json.loads(path.read_text(encoding='utf-8'))


def review_identity(root,sample,link_id,decision,reviewer,evidence):
    _,config=load_config(root,sample)
    edge=next((e for e in identity_links(root,sample)['edges'] if e['id']==link_id),None)
    if not edge or edge['reference_end_frame'] is None:raise ValueError('该片段没有可复核的前序关联')
    if decision not in ('same','different','unknown') or not isinstance(reviewer,str) or not reviewer.strip() or not isinstance(evidence,str) or not evidence.strip():
        raise ValueError('请选择关联意见，并填写复核人和判断依据')
    items=config.setdefault('identity_reviews',[])
    items[:]=[r for r in items if (r['worm_id'],r['start_frame'],r['end_frame'])!=(edge['worm_id'],edge['start_frame'],edge['end_frame'])]
    items.append(dict(id=secrets.token_hex(8),worm_id=edge['worm_id'],start_frame=edge['start_frame'],end_frame=edge['end_frame'],
        reference_start_frame=edge['reference_start_frame'],reference_end_frame=edge['reference_end_frame'],
        decision=decision,reviewer=reviewer.strip(),evidence=evidence.strip(),confirmed_at=datetime.now(timezone.utc).isoformat()))
    return derive_geometry_run(root,sample,config)


def remove_identity_review(root,sample,review_id):
    _,config=load_config(root,sample);items=config.get('identity_reviews',[])
    remaining=[r for r in items if r['id']!=review_id]
    if len(items)==len(remaining):raise ValueError('身份复核记录不存在')
    config['identity_reviews']=remaining
    return derive_geometry_run(root,sample,config)


def identity_frame(root,sample,worm,frame):
    import cv2
    import numpy as np
    base,config=load_config(root,sample)
    from analyze_phenotypes import load_sample
    _,source,ids,get_mask,_=load_sample(sample,apply_guards=False)
    if type(worm) is not int or worm not in ids or type(frame) is not int or not 0<=frame<source['frames']:raise ValueError('复核画面帧号或虫编号无效')
    cap=cv2.VideoCapture(source['video']);cap.set(cv2.CAP_PROP_POS_FRAMES,frame);ok,image=cap.read();cap.release()
    if not ok:raise ValueError('原始帧无法读取')
    x0,y0,x1,y1=source['crop_xyxy'];image=image[y0:y1,x0:x1].copy();mask=get_mask(worm,frame)
    contours,_=cv2.findContours(mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(image,contours,-1,(50,220,90),2)
    from centerline_methods import method_source
    path=base/('phenotypes-'+method_source(config['centerline_method']))/'diagnostic-centerlines.npz'
    if path.is_file():
        with np.load(path,allow_pickle=False) as lines:
            key=f'w{worm}_f{frame}'
            if key in lines:cv2.polylines(image,[np.rint(lines[key]).astype(np.int32)],False,(255,255,255),2)
    yy,xx=np.nonzero(mask)
    if len(xx):image=image[max(0,yy.min()-70):min(len(image),yy.max()+71),max(0,xx.min()-70):min(image.shape[1],xx.max()+71)]
    image=cv2.resize(image,(600,max(1,round(image.shape[0]*600/image.shape[1]))))
    cv2.putText(image,f'Frame {frame} | W{worm} | source + mask + candidate pose',(10,25),cv2.FONT_HERSHEY_SIMPLEX,.5,(30,60,230),1,cv2.LINE_AA)
    ok,encoded=cv2.imencode('.jpg',image)
    if not ok:raise ValueError('复核图像编码失败')
    return encoded.tobytes()


def derive_geometry_run(root,sample,config=None):
    """Recompute metrics in a new run, provided all geometric inputs are identical."""
    from run_storage import fresh_sample,input_identity
    from worm_pipeline import complete,build_stages,validate_config
    old,original=load_config(root,sample)
    config=copy.deepcopy(config if config is not None else original)
    def geometry_inputs(c):
        return {k:v for k,v in c.items() if k not in ('head_annotations','head_detection','identity_reviews','previous_sample')}
    if geometry_inputs(config)!=geometry_inputs(original):
        raise ValueError('几何输入发生变化，不能复用分割与姿态')
    checked,info=validate_config(config)
    stages=build_stages(checked,info,old/'pipeline-config.json',root/'config/phenotype_config.json')
    reusable=[s for s in stages if s['phase'] not in ('metrics','export')]
    if original.get('centerline_method') != checked['centerline_method']:
        reusable = [s for s in reusable if s['phase'] != 'fit']
    if checked['centerline_method']=='body_sequence' and any(
            original.get(key)!=config.get(key) for key in ('head_annotations','head_detection')):
        reusable=[s for s in reusable if s['phase']!='fit']
    if checked['centerline_method'] == 'self_contact' and any(
            original.get(key) != config.get(key) for key in ('head_annotations','identity_reviews')):
        # A head anchor protects its frame during self-contact fitting. Identity reviews
        # change the segment boundaries that constrain which poses may be joined.
        reusable = [s for s in reusable if s['phase'] != 'fit']
    replay = next((s for s in reusable if s['name']=='crop-recovery'),None)
    repeat_replay = replay is not None and (
        not Path(replay['outputs'][0]).is_file() or original.get('identity_reviews') != config.get('identity_reviews'))
    if repeat_replay:
        reusable = [s for s in reusable if s['name']!='crop-recovery' and s['phase']!='fit']
    if not all(complete(s,checked,info) for s in reusable):
        raise ValueError('原运行的算法版本或产物不再匹配，请先继续原任务完成重算')
    from cache_integrity import matches,snapshot
    state_path=old/'pipeline-state.json'
    old_state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    for stage in reusable:
        if not matches(stage,old,old_state.get('stages',{}).get(stage['name'],{})):
            raise ValueError('缓存内容与保存的校验值不同，不能复用；请从原输入新建任务重算')
    config=copy.deepcopy(checked);config['sample']=fresh_sample(sample,root/'outputs')
    config['previous_sample']=sample
    base=root/'outputs'/config['sample'];base.mkdir(parents=True)
    shutil.copytree(old/'sam2-tracking',base/'sam2-tracking',ignore=shutil.ignore_patterns('frames-*','.crop-replay-*',*(['crop-recovery'] if repeat_replay else [])))
    # Retain every available pose/evidence folder used by this method.
    folders={Path(s['outputs'][0]).resolve().relative_to(old).parts[0] for s in reusable if s['phase'] in ('fit','repair','temporal')}
    for folder in folders:
        shutil.copytree(old/folder,base/folder)
        mask_source=base/folder/'mask-source.json'
        if mask_source.exists():mask_source.write_text(json.dumps(dict(mask_source=str((base/'sam2-tracking').resolve()))),encoding='utf-8')
    (base/'pipeline-config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    state=dict(sample=config['sample'],completed_through=reusable[-1]['phase'],
        identity=input_identity(config,root/'config/phenotype_config.json',motion_constraints_path(root)),
        stages={s['name']:dict(status='complete',reused_from=sample) for s in reusable})
    for stage in build_stages(config,info,base/'pipeline-config.json',root/'config/phenotype_config.json'):
        if stage['name'] in state['stages']:
            verified=bool(old_state.get('stages',{}).get(stage['name'],{}).get('output_sha256'))
            state['stages'][stage['name']].update(output_sha256=snapshot(stage,base),
                integrity_origin='verified_source_snapshot' if verified else 'legacy_structural_check_then_snapshot')
    (base/'pipeline-state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    return config


def restore_saved_timing(progress,base,sample):
    # Historical durations must come from the completed record, never the active
    # session's clock (which may belong to a different video).
    import math
    saved = {}
    try:
        record = json.loads((base/'pipeline-progress.json').read_text(encoding='utf-8'))
        if isinstance(record, dict) and record.get('sample') == sample and record.get('status') == 'complete':
            saved = record
    except (OSError, ValueError):
        pass
    def recorded_number(key):
        value = saved.get(key)
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    progress.started=progress.ended=progress.active_started=None
    progress.data.update(history_reopened=True, saved_elapsed_seconds=recorded_number('elapsed_seconds'),
                         saved_finished_at=recorded_number('finished_at'), device=saved.get('device') or '')


def result_video_files(base, config):
    from centerline_methods import method_source
    folder = base / ('phenotypes-' + method_source(config['centerline_method']))
    return [('video', '标注视频', folder / f'{config["sample"]}_annotated_full-frame.mp4', 'export'),
            ('source-video', '原始视频（只读）', Path(config['video']), 'metrics')]


def saved_videos(base, config):
    stages = json.loads((base / 'pipeline-state.json').read_text(encoding='utf-8')).get('stages', {})
    return {key: (label, path) for key, label, path, stage in result_video_files(base, config)
            if stages.get(stage, {}).get('status') in ('complete', 'reused') and path.is_file()}


def chamber_summaries(root, items):
    from centerline_methods import method_source
    from result_metrics import phenotype_results
    reports = {}
    for item in items:
        if item['status'] != 'complete':
            continue
        sample = item['sample']
        try:
            base, config = load_config(root, sample)
            folder = base / ('phenotypes-' + method_source(config['centerline_method']))
            data = phenotype_results(folder)
            if not data['worms']:
                raise ValueError('缺少线虫指标')
            data.update(chamber_id=item.get('chamber_id'), video_name=item['video_name'])
            reports[sample] = data
            try:
                data['videos'] = [dict(key=key, label=label) for key, (label, _) in saved_videos(base, config).items()]
            except (OSError, ValueError):
                data['videos'] = []
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            reports[sample] = {'error': '指标文件缺失或无法读取'}
    return reports


def saved_timeline(root, sample, *, trend_only=True):
    from centerline_methods import method_source
    from frame_metrics import load_frame_metrics
    base, config = load_config(root, sample)
    folder = base / ('phenotypes-' + method_source(config['centerline_method']))
    summary = json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    return dict(load_frame_metrics(folder/'per_frame_metrics.csv', summary['fps'],
                                   summary['worms'][0]['frames'], trend_only=trend_only), sample=sample,
                crop_xyxy=summary.get('crop_xyxy') or config.get('crop_xyxy'))


def reopen(progress,root,sample):
    base,config=load_config(root,sample)
    from centerline_methods import method_source, METHODS
    report=base/('phenotypes-'+method_source(config['centerline_method']))/'summary.json'
    if not report.is_file(): raise ValueError('尚无完整指标，请选择继续任务')
    summary=json.loads(report.read_text(encoding='utf-8'))
    state=json.loads((base/'pipeline-state.json').read_text(encoding='utf-8'))
    completed=[(k,v) for k,v in state.get('stages',{}).items() if v.get('status')=='complete']
    if not any(k=='metrics' for k,_ in completed): raise ValueError('指标阶段尚未完成')
    progress.path=progress.event_path=progress.log_path=None
    restore_saved_timing(progress,base,sample)
    progress.preview_dir=None
    progress.artifacts={};progress.frame_metrics=None;progress.config=config
    progress.data.update(sample=sample,video_name=Path(config['video']).name,
        frames=summary['worms'][0]['frames'],fps=summary['fps'],worm_count=len(summary['worms']),
        method=METHODS[config['centerline_method']]['label'],output_dir=str(base),read_only=False,
        error=None,notice='已重新打开保存结果，未重新计算或改写历史文件。',artifacts=[])
    progress.data['stages']=[dict(name=k,phase=k,label=k,status='complete',fraction=1,seconds=v.get('seconds',0))
        for k,v in completed if k in ('metrics','export')]
    progress.finish(base,config,'export' if any(k=='export' for k,_ in completed) else 'metrics')
    progress.server.annotation=None


def save_draft(annotation,body):
    folder=draft_folder(annotation);folder.mkdir(parents=True,exist_ok=True)
    # One atomic latest snapshot; old manually saved revisions remain untouched.
    record=dict(identity=annotation.identity,body=body,saved_at=time.time())
    path=folder/'latest.json'
    temp=folder/f'.{secrets.token_hex(8)}.tmp'
    try:
        temp.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        temp.replace(path)
    finally: temp.unlink(missing_ok=True)
    return dict(saved=True,revision=path.name)


def get_draft(annotation):
    for path in sorted(draft_folder(annotation).glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True):
        try: record=json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,OSError): continue
        if record.get('identity')==annotation.identity:
            if record.get('body',{}).get('submitted'): return dict(body=None)
            return record
    return dict(body=None)


def draft_folder(annotation):
    signature=hashlib.sha256(json.dumps(annotation.identity,sort_keys=True).encode()).hexdigest()
    if getattr(annotation, 'chamber_mode', False):
        signature += '-chambers'
    return annotation.output.parent.parent/'annotation-drafts'/signature
