"""Actionable review intervals from saved evidence; never grant permissions."""
from bisect import bisect_left, bisect_right
from collections import defaultdict

from quality_schema import flag, permissions
from measurement_units import number

VERSION = 'single-worm-review-intervals-v2'


def review_kind(row):
    if not flag(row,'source_valid'):
        return 'source'
    if not flag(row,'mask_valid'):
        return 'mask'
    if not flag(row,'identity_valid'):
        return 'identity'
    if not flag(row,'pose_valid'):
        return 'pose'
    if not flag(row,'scale_valid') and row.get('measurement_mode')!='pixels':
        return 'scale'
    if flag(row,'head_anchor_conflict'):
        return 'head_conflict'
    if not permissions(row)['head']:
        return 'head'
    if row.get('head_angle_method')=='review_geometry':
        return 'head_geometry'
    return None


def head_reviewable(row):
    return (permissions(row)['posture'] and row.get('centerline_topology','simple')=='simple'
            and not flag(row,'endpoint_a_inferred') and not flag(row,'endpoint_b_inferred')
            and not row.get('display_tip_source'))


def build_intervals(rows):
    """One task per continuous issue and evidence segment, with a clear keyframe."""
    groups=defaultdict(list)
    for row in rows:
        groups[int(row['worm_id'])].append(row)
    tasks=[]
    recovered=0
    recovered_heads=0
    for worm, group in sorted(groups.items()):
        worm_tasks=[]
        group.sort(key=lambda r:int(r['frame']))
        clear=[int(r['frame']) for r in group if head_reviewable(r)]
        observed_angles=defaultdict(list)
        current=None
        for row in group:
            frame=int(row['frame']);kind=review_kind(row)
            recovered+=bool(row.get('body_recovery_source') and permissions(row)['posture'])
            recovered_heads+=row.get('head_detection_status')=='recovered_from_user_point' and permissions(row)['head']
            segment=(row.get('identity_segment'),row.get('body_sequence_segment',row.get('head_segment')))
            if row.get('head_angle_method') in ('centerline_chords','section_fit') and kind is None:
                observed_angles[(*segment,row['head_angle_block'])].append(frame)
            if kind=='head_geometry':segment=(*segment,row['head_angle_block'])
            if kind is None:
                current=None
                continue
            if current is None or current['_segment']!=segment or current['kind']!=kind or current['end']!=frame-1:
                current=dict(worm_id=worm,start=frame,end=frame,kind=kind,target=None,
                             _segment=segment,_rank=None)
                worm_tasks.append(current)
            current['end']=frame
            if kind=='head' and head_reviewable(row):
                # Prefer well-separated observed ends, then mask support. These
                # are selection criteria, not a probability of anatomical truth.
                extension=number(row.get('body_extension'))
                support=number(row.get('centerline_inside_fraction'))
                rank=(extension if extension is not None else -1,
                      support if support is not None else -1)
                if current['_rank'] is None or rank>current['_rank']:
                    current['target']=frame;current['_rank']=rank
        for task in worm_tasks:
            references=observed_angles[task['_segment']] if task['kind']=='head_geometry' else clear
            left=bisect_left(references,task['start'])-1;right=bisect_right(references,task['end'])
            task['before']=references[left] if left>=0 else None
            task['after']=references[right] if right<len(references) else None
            task['action']=('confirm_head' if task['target'] is not None else
                            'inspect_anchors' if task['kind']=='head_conflict' else 'inspect')
            if task['kind']=='head_geometry':task['target']=(task['start']+task['end'])//2
            if task['target'] is None:task['target']=task['start']
            task.pop('_rank');task.pop('_segment')
        tasks.extend(worm_tasks)
    return dict(version=VERSION,intervals=tasks,automatic_recovered_frames=recovered,head_recovered_from_mark_frames=recovered_heads,
                head_confirmation_intervals=sum(t['action']=='confirm_head' for t in tasks),
                scope='review workload estimate; not accuracy or observed human actions')
