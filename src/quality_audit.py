"""Explain measurement attrition without changing any scientific gate."""
import csv
import json
from collections import Counter
from quality_schema import flag,permissions

VERSION='quality-attrition-v1'

def intervals(frames):
    result=[]
    for frame in sorted(set(frames)):
        if result and frame==result[-1][1]+1:result[-1][1]=frame
        else:result.append([frame,frame])
    return result

def evidence(value):
    if isinstance(value,dict):return value
    try:
        parsed=json.loads(value or '{}')
        return parsed if isinstance(parsed,dict) else {}
    except (TypeError,ValueError):return {}

def audit(rows):
    worms={}
    for worm in sorted({int(r['worm_id']) for r in rows}):
        group=[r for r in rows if int(r['worm_id'])==worm]
        geometry=[r for r in group if flag(r,'source_valid') and flag(r,'mask_valid') and flag(r,'pose_valid')]
        blocked=[r for r in geometry if not flag(r,'identity_valid')]
        triggers=[]
        for row in group:
            e=evidence(row.get('identity_evidence'));frame=int(row['frame'])
            if e.get('event_frame')==frame and row.get('identity_status') in ('jump_review','contact_ambiguous'):
                triggers.append(dict(frame=frame,status=row['identity_status'],evidence=e))
        worms[str(worm)]=dict(frames=len(group),source_valid=sum(flag(r,'source_valid') for r in group),
            mask_valid=sum(permissions(r)['area'] for r in group),geometric_pose_valid=len(geometry),
            formal_posture_valid=sum(permissions(r)['posture'] for r in group),
            identity_valid=sum(flag(r,'identity_valid') for r in group),
            identity_auto_reassociated=sum(r.get('identity_link_source')=='auto_reassociated' for r in group),
            identity_human_confirmed=sum(flag(r,'identity_human_confirmed') for r in group),
            identity_blocked_geometry=len(blocked),identity_blocked_ranges=intervals(int(r['frame']) for r in blocked),
            tracklet_pose_valid=sum(flag(r,'tracklet_pose_valid') for r in group),
            tracklet_additional_pose=sum(flag(r,'tracklet_pose_valid') and not flag(r,'identity_valid') for r in group),
            tracklet_motion_valid=sum(flag(r,'tracklet_motion_valid') for r in group),
            formal_posture_rejected_ranges=intervals(int(r['frame']) for r in group if not permissions(r)['posture']),
            head_confirmed=sum(flag(r,'head_identity_confirmed') for r in group),
            head_conflict_frames=sum(flag(r,'head_anchor_conflict') for r in group),
            identity_status_counts=dict(Counter(r.get('identity_status','missing') for r in group)),triggers=triggers)
    return dict(version=VERSION,meaning='Rule acceptance and gating; not biological accuracy',worms=worms)

def write_audit(output,rows):
    result=audit(rows)
    (output/'quality-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/'quality-gates.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=['frame','worm_id','source_valid','mask_valid','pose_valid',
            'identity_valid','formal_posture_valid','head_confirmed','head_conflict','identity_trigger_frame','quality_reason'])
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(frame=r['frame'],worm_id=r['worm_id'],
                **{k:flag(r,k) for k in ('source_valid','mask_valid','pose_valid','identity_valid')},
                formal_posture_valid=permissions(r)['posture'],head_confirmed=flag(r,'head_identity_confirmed'),
                head_conflict=flag(r,'head_anchor_conflict'),
                identity_trigger_frame=evidence(r.get('identity_evidence')).get('event_frame'),quality_reason=r.get('quality_reason','')))
    return result
