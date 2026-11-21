"""Explain measurement attrition without changing any scientific gate."""
import csv
import json
from collections import Counter, defaultdict
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
    groups = defaultdict(list)
    for row in rows:
        groups[int(row['worm_id'])].append(row)
    worms = {}
    for worm, group in sorted(groups.items()):
        counts = Counter()
        statuses = Counter()
        blocked_frames = []
        rejected_frames = []
        triggers = []
        for row in group:
            frame = int(row['frame'])
            allowed = permissions(row)
            source_valid = flag(row, 'source_valid')
            identity_valid = flag(row, 'identity_valid')
            geometry_valid = source_valid and flag(row, 'mask_valid') and flag(row, 'pose_valid')
            tracklet_pose = flag(row, 'tracklet_pose_valid')
            counts['source_valid'] += source_valid
            counts['mask_valid'] += allowed['area']
            counts['geometric_pose_valid'] += geometry_valid
            counts['formal_posture_valid'] += allowed['posture']
            counts['identity_valid'] += identity_valid
            counts['identity_auto_reassociated'] += row.get('identity_link_source') == 'auto_reassociated'
            counts['identity_human_confirmed'] += flag(row, 'identity_human_confirmed')
            counts['tracklet_pose_valid'] += tracklet_pose
            counts['tracklet_additional_pose'] += tracklet_pose and not identity_valid
            counts['tracklet_motion_valid'] += flag(row, 'tracklet_motion_valid')
            counts['head_confirmed'] += flag(row, 'head_identity_confirmed')
            counts['head_conflict_frames'] += flag(row, 'head_anchor_conflict')
            statuses[row.get('identity_status', 'missing')] += 1
            if geometry_valid and not identity_valid:
                blocked_frames.append(frame)
            if not allowed['posture']:
                rejected_frames.append(frame)
            event = evidence(row.get('identity_evidence'))
            if event.get('event_frame') == frame and row.get('identity_status') in ('jump_review', 'contact_ambiguous'):
                triggers.append(dict(frame=frame, status=row['identity_status'], evidence=event))
        worms[str(worm)] = dict(frames=len(group), **counts,
            identity_blocked_geometry=len(blocked_frames), identity_blocked_ranges=intervals(blocked_frames),
            formal_posture_rejected_ranges=intervals(rejected_frames),
            identity_status_counts=dict(statuses), triggers=triggers)
    return dict(version=VERSION, meaning='Rule acceptance and gating; not biological accuracy', worms=worms)

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
