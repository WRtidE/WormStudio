"""Measure PPT-defined phenotypes from saved SAM2 masks, without re-segmenting.

Run in Conda WormStudio: python src/analyze_phenotypes.py --sample all
Default: latest saved contact correction, or temporal WormTracer centerlines.
Each corrected fit selects its matching mask source through mask-source.json.
Use --centerline-method skeleton to reproduce the original baseline.
Head coordinates in phenotype_config.json: {"2-1-1314:1": [x, y], ...}.
Coordinates are within the saved cropped first frame, not the full video.
"""
import argparse
from pipeline_progress import report_progress
from live_preview import report_preview
import csv
import json
import math
import shutil
import tempfile
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage
from scipy.signal import find_peaks, savgol_filter
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from skimage.morphology import skeletonize
from centerline_topology import geometry_quality
from worm_colors import worm_color
from head_tail import detect_head_tail, endpoint_labels, CANDIDATE_STATUSES, VERSION as HEAD_TAIL_VERSION
from display_centerlines import build_display, VERSION as DISPLAY_VERSION
from quality_schema import VERSION as QUALITY_VERSION, tracking_dimensions, finish_dimensions, permissions, exclude_unusable, describe

from resource_paths import PROJECT_ROOT as ROOT
# 导出视频中的中心线宽度（原来为 3 像素）；可在这里调整。
CENTERLINE_WIDTH_PX = 2


def finite_stat(values, statistic=np.median):
    data=np.asarray(values,dtype=float)
    data=data[np.isfinite(data)]
    return float(statistic(data)) if len(data) else np.nan


def json_safe(value):
    if isinstance(value,dict):return {k:json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [json_safe(v) for v in value]
    if isinstance(value,(float,np.floating)):return float(value) if np.isfinite(value) else None
    if isinstance(value,np.integer):return int(value)
    return value


def read_image(path, gray=False):
    im = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), np.uint8), 0 if gray else 1)
    if im is None:
        raise ValueError(f"Cannot decode {path}")
    return im


def write_png(path, image):
    Path(path).write_bytes(cv2.imencode('.png', image)[1].tobytes())


def resample(points, count=101):
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    keep = np.r_[True, np.diff(s) > 1e-8]
    if s[-1] < 1:
        raise ValueError('Degenerate centerline')
    return np.column_stack([np.interp(np.linspace(0, s[-1], count), s[keep], points[keep, j]) for j in (0, 1)])


def centerline(mask):
    """Longest geodesic between skeleton endpoints; distances include diagonals."""
    ys, xs = np.nonzero(mask)
    if len(xs) < 20:
        raise ValueError('Empty or very small mask')
    x0, y0 = max(0, xs.min()-3), max(0, ys.min()-3)
    local = mask[y0:ys.max()+4, x0:xs.max()+4]
    labels, n = ndimage.label(local)
    sizes = np.bincount(labels.ravel()); sizes[0] = 0
    local = labels == sizes.argmax()
    sk = skeletonize(local)
    coords = np.argwhere(sk)
    index = np.full(sk.shape, -1, int); index[sk] = np.arange(len(coords))
    rows, cols, weights = [], [], []
    for dy, dx in [(0, 1), (1, -1), (1, 0), (1, 1)]:
        q = coords + [dy, dx]
        valid = (q[:, 0]>=0)&(q[:, 0]<sk.shape[0])&(q[:, 1]>=0)&(q[:, 1]<sk.shape[1])
        a = np.flatnonzero(valid); b = index[q[valid,0], q[valid,1]]
        a, b = a[b>=0], b[b>=0]
        rows.extend(a); cols.extend(b); weights.extend([math.hypot(dx,dy)]*len(a))
        rows.extend(b); cols.extend(a); weights.extend([math.hypot(dx,dy)]*len(a))
    graph = coo_matrix((weights,(rows,cols)),shape=(len(coords),len(coords))).tocsr()
    ends = np.flatnonzero(np.diff(graph.indptr)==1)
    if len(ends)<2:
        raise ValueError('Closed/self-contact skeleton: no reliable pair of endpoints')
    distances, predecessors = dijkstra(graph, indices=ends, return_predecessors=True)
    end_dist = distances[:,ends]
    end_dist[~np.isfinite(end_dist)] = -1
    a,b = np.unravel_index(np.argmax(end_dist),end_dist.shape)
    node = int(ends[b]); route = [node]
    while node != ends[a]:
        node = int(predecessors[a,node])
        if node<0: raise ValueError('Disconnected centerline')
        route.append(node)
    points = coords[route][:,::-1].astype(float)
    # Extend both terminal tangents to the mask boundary to include the tips.
    for reverse in (False,True):
        if reverse: points=points[::-1]
        tangent=points[0]-points[min(8,len(points)-1)]
        tangent/=max(np.linalg.norm(tangent),1e-9)
        tip=points[0].copy()
        for step in np.arange(.5,35,.5):
            candidate=points[0]+step*tangent
            xi,yi=np.rint(candidate).astype(int)
            if not (0<=yi<local.shape[0] and 0<=xi<local.shape[1] and local[yi,xi]):break
            tip=candidate
        points=np.vstack([tip,points])
        if reverse: points=points[::-1]
    points=resample(points)
    smooth=savgol_filter(points,7,2,axis=0)
    smooth[0],smooth[-1]=points[0],points[-1]
    points=resample(smooth)
    dt=ndimage.distance_transform_edt(local)
    radius=ndimage.map_coordinates(dt,[points[:,1],points[:,0]],order=1,mode='constant')
    inside=ndimage.map_coordinates(local.astype(float),[points[:,1],points[:,0]],order=0,mode='constant')
    points += [x0,y0]
    return points,radius,dict(skeleton_endpoints=len(ends),
                            skeleton_path_fraction=len(route)/len(coords),
                            centerline_inside_fraction=float(inside.mean()))


def signed_angle(points):
    # Head-outward tangent (0-10%) relative to the neck-outward tangent (10-30%).
    a=points[0]-points[10]; b=points[10]-points[30]
    return float(np.degrees(np.arctan2(b[0]*a[1]-b[1]*a[0],np.dot(b,a))))


def frustum_volume(points,radius):
    ds=np.linalg.norm(np.diff(points,axis=0),axis=1)
    return float(np.sum(np.pi*ds*(radius[:-1]**2+radius[:-1]*radius[1:]+radius[1:]**2)/3))


def runs(valid):
    indices=np.flatnonzero(valid)
    return np.split(indices,np.where(np.diff(indices)>1)[0]+1) if len(indices) else []


def smooth_valid(values,valid,window):
    result=np.full_like(values,np.nan,dtype=float)
    for idx in runs(valid):
        w=min(window,len(idx) if len(idx)%2 else len(idx)-1)
        result[idx]=savgol_filter(values[idx],w,2,axis=0) if w>=5 else values[idx]
    return result


def bend_events(angles,valid,fps,threshold,min_interval,timestamps=None):
    if timestamps is None:timestamps=np.arange(len(valid))/fps
    events=[]; cycles=0
    for idx in runs(valid):
        if len(idx)<3:continue
        signal=angles[idx]
        distance=max(1,int(round(min_interval*fps)))
        positive=find_peaks(signal,height=threshold,prominence=threshold/2,distance=distance)[0]
        negative=find_peaks(-signal,height=threshold,prominence=threshold/2,distance=distance)[0]
        candidates=sorted([(int(i),1) for i in positive]+[(int(i),-1) for i in negative])
        selected=[]
        for i,sign in candidates:
            if selected and selected[-1][1]==sign:
                if abs(signal[i])>abs(signal[selected[-1][0]]):selected[-1]=(i,sign)
            else:selected.append((i,sign))
        cycles+=max(0,len(selected)-1)//2
        events.extend(dict(frame=int(idx[i]),time_s=float(timestamps[idx[i]]),direction=sign,
                           angle_deg=float(signal[i])) for i,sign in selected)
    duration=sum(timestamps[idx[-1]]-timestamps[idx[0]] for idx in runs(valid))
    return events,cycles,duration


def calibration(sample,summary,config):
    if summary.get('measurement_mode') == 'pixels':
        return 1., {'source':'User selected pixel units; no physical calibration', 'status':'pixels', 'um_per_px':None}
    override=config['scale_um_per_px'].get(sample)
    if override:return float(override),{'source':'user-config','um_per_px':float(override)}
    if 'scale_um_per_px' in summary:
        return summary['scale_um_per_px'],{'source':summary['scale_source'],'status':summary.get('scale_status','supplied'),
            'um_per_px':summary['scale_um_per_px']}
    # PPT specifies pillar diameter=80 um. Use isolated near-circular AMG objects.
    with (ROOT/'outputs'/sample/'multiworm-amg'/'classifier-scores.csv').open() as handle:
        candidates=list(csv.DictReader(handle))
    diameters=[]; centers=[]
    for row in candidates:
        area=int(row['area']); w=int(row['x1'])-int(row['x0'])+1; h=int(row['y1'])-int(row['y0'])+1
        if not (3500<area<5000 and .9<w/h<1.1):continue
        xy=np.array([(int(row['x0'])+int(row['x1']))/2,(int(row['y0'])+int(row['y1']))/2])
        if any(np.linalg.norm(xy-c)<20 for c in centers):continue
        centers.append(xy);diameters.append(2*np.sqrt(area/np.pi))
    if len(diameters)<5:raise ValueError('Insufficient pillars for scale; set scale_um_per_px in config')
    median=float(np.median(diameters));scale=80/median
    return scale,{'source':'PPT pillar diameter 80 um / median equivalent diameter of cached AMG circles',
                  'status':'estimated_assuming_same_chip_as_PPT','pillar_count':len(diameters),
                  'median_diameter_px':median,'diameter_iqr_px':float(np.subtract(*np.percentile(diameters,[75,25])))}


def load_sample(sample,mask_source=None,apply_guards=True):
    base=ROOT/'outputs'/sample
    default_source='sam2-tracking' if (base/'pipeline-config.json').exists() else ('multiworm-tracking-manual' if sample=='2-1-1314' else 'sam2-tracking')
    data=Path(mask_source) if mask_source is not None else base/default_source
    summary=json.loads((data/'summary.json').read_text(encoding='utf-8'))
    summary['mask_source']=str(data)
    n=summary['frames'];bad={}
    if (data/'masks').is_dir():
        with (data/'tracking-quality.csv').open() as handle:
            for row in csv.DictReader(handle):
                bad[(int(row['frame']),int(row['worm_id']))]=row['low_confidence'].lower()=='true'
        def get_mask(worm,frame):return read_image(data/'masks'/f'worm-{worm}'/f'{frame:06d}.png',True)>0
        ids=summary.get('worm_ids') or sorted(int(p.name.split('-')[-1]) for p in (data/'masks').glob('worm-*'))
    else:
        with np.load(data/'masks-packed.npz') as packed:shape=tuple(packed['shape']);packed_masks=packed['masks']
        def get_mask(worm,frame):return np.unpackbits(packed_masks[frame],count=int(np.prod(shape))).reshape(shape).astype(bool)
        ids=[1]
    if apply_guards:
        from quality_stage import load_guards
        snapshot=base/'pipeline-config.json'
        config=json.loads(snapshot.read_text(encoding='utf-8')) if snapshot.is_file() else {}
        guards=load_guards(data,ids,n,required=bool(config.get('quality_schema_version')))
        if guards is not None:
            for worm,records in guards.items():
                for record in records:
                    key=(record['frame'],worm)
                    bad[key]=bad.get(key,False) or not record['mask_valid']
    return data,summary,ids,get_mask,bad


def fitted_mask_source(sample,source_name):
    manifest=ROOT/'outputs'/sample/source_name/'mask-source.json'
    return json.loads(manifest.read_text(encoding='utf-8'))['mask_source'] if manifest.exists() else None


def is_circle_path(path,body_length,fps,config,timestamps=None):
    d=np.diff(path,axis=0);step=np.linalg.norm(d,axis=1)
    intervals=np.diff(timestamps) if timestamps is not None else 1/fps
    keep=step>config['stationary_speed_bl_s']*body_length*intervals
    if keep.sum()<.8*len(step):return False
    orientation=np.unwrap(np.arctan2(d[keep,1],d[keep,0]));changes=np.diff(orientation)
    rotation=abs(np.degrees(orientation[-1]-orientation[0]))
    coherence=abs(changes.sum())/max(np.abs(changes).sum(),1e-9)
    closure=np.linalg.norm(path[-1]-path[0])/body_length
    x,y=path.T
    area=abs(np.dot(x,np.roll(y,-1))-np.dot(y,np.roll(x,-1)))/2
    compactness=area/max(step.sum()**2,1e-9)
    return (rotation>=config['circling_angle_deg'] and closure<=config['circling_closure_bl']
            and step.sum()>body_length*.5 and compactness>=config['circling_min_compactness']
            and coherence>=config['circling_min_direction_coherence'])


def pose_length_reference(rows):
    """Keep the geometry gate anchored to pre-association continuity evidence."""
    return finite_stat([r['length_um'] for r in rows if r['centerline_valid']
        and r.get('identity_original_valid',r['identity_valid'])])


def analyze(sample,config,method='skeleton',wormtracer_source='wormtracer'):
    data,source,ids,get_mask,bad=load_sample(sample,fitted_mask_source(sample,wormtracer_source) if method!='skeleton' else None)
    output=ROOT/'outputs'/sample/('phenotypes-'+wormtracer_source if method!='skeleton' else 'phenotypes');output.mkdir(parents=True,exist_ok=True)
    scale,scale_info=calibration(sample,source,config)
    fps=source['fps'];n=source['frames'];all_rows=[];all_summaries=[];all_events=[];lines={}
    from video_timing import verify_timebase
    timebase=verify_timebase(source['video'],n,fps)
    fps=timebase['fps']
    times=np.asarray(timebase['timestamps_s'])
    snapshot=ROOT/'outputs'/sample/'pipeline-config.json'
    video_config=json.loads(snapshot.read_text(encoding='utf-8')) if snapshot.is_file() else {}
    from quality_stage import load_guards
    guards=load_guards(data,ids,n,required=bool(video_config.get('quality_schema_version')))
    from identity_reassociation import reassociate,VERSION as ASSOCIATION_VERSION,RULES
    association_report=dict(version=ASSOCIATION_VERSION,rules=RULES,edges=[],scope='legacy guards unavailable')
    if guards is not None:
        fit_qualities={}
        for worm in ids:
            path=ROOT/'outputs'/sample/wormtracer_source/f'worm-{worm}'/'quality.json'
            if method!='skeleton' and path.is_file():fit_qualities[worm]=json.loads(path.read_text(encoding='utf-8'))
        guards,association_report=reassociate(guards,source,get_mask,fit_qualities,
            reviews=video_config.get('identity_reviews',[]))
    (output/'identity-links.json').write_text(json.dumps(json_safe(association_report),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    diagnostics=[];tracklet_reports=[];display_lines={}
    for worm in ids:
        fitted=np.load(ROOT/'outputs'/sample/wormtracer_source/f'worm-{worm}'/'centerlines.npy') if method!='skeleton' else None
        if fitted is not None and fitted.shape!=(n,101,2):raise ValueError('WormTracer frame/point count mismatch')
        quality_path=ROOT/'outputs'/sample/wormtracer_source/f'worm-{worm}'/'quality.json'
        fit_quality=json.loads(quality_path.read_text(encoding='utf-8')) if fitted is not None and quality_path.exists() else None
        if fit_quality is not None and (len(fit_quality)!=n or any(q['frame']!=i for i,q in enumerate(fit_quality))):
            raise ValueError('Centerline quality frame mismatch')
        rows=[];previous=None;previous_center=None
        radius_profiles=np.full((n,101),np.nan)
        head_point=config.get('head_points_crop_xy',{}).get(f'{sample}:{worm}')
        manual=video_config.get('head_annotations',{}).get(str(worm))
        if manual is None and head_point is not None: manual=dict(frame=0,point=head_point)
        head_status='unconfirmed_A_B_endpoints'
        initial_width_ratio=None
        for frame in range(n):
            done=ids.index(worm)*n+frame
            report_progress(done,n*len(ids),f'计算线虫 {worm} 的逐帧表型',unit='虫帧',fraction=.6*done/(n*len(ids)))
            mask=get_mask(worm,frame);ys,xs=np.nonzero(mask)
            row={'sample':sample,'worm_id':worm,'frame':frame,'time_s':timebase['timestamps_s'][frame],
                 'mask_bbox_crop_xyxy':json.dumps([int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)]) if len(xs) else '',
                 'time_valid':True,'time_status':timebase['status'],
                 'centroid_x_source_px':float(xs.mean()+source['crop_xyxy'][0]) if len(xs) else np.nan,
                 'centroid_y_source_px':float(ys.mean()+source['crop_xyxy'][1]) if len(ys) else np.nan,
                 'area_um2':float(mask.sum()*scale**2),'centroid_x_um':float(xs.mean()*scale) if len(xs) else np.nan,
                 'centroid_y_um':float(ys.mean()*scale) if len(ys) else np.nan,
                 'head_status':head_status,'head_identity_confirmed':False,
                 'mask_valid':not bad.get((frame,worm),False),'centerline_valid':False,
                 'valid_for_summary':False,'quality_reason':'',
                 'length_um':np.nan,'volume_estimate_um3':np.nan,'mean_width_um':np.nan,
                 'head_candidate_angle_deg':np.nan,'opposite_end_angle_deg':np.nan}
            guard=guards[worm][frame] if guards is not None else dict(frame=frame,worm_id=worm,
                mask_valid=not bad.get((frame,worm),False),reasons=['sam2_tracking_quality'] if bad.get((frame,worm),False) else [],
                identity_review=len(ids)>1,identity_valid=len(ids)==1,
                identity_status='legacy_unassessed' if len(ids)>1 else 'provisional_continuous',
                identity_reason='identity_unverified' if len(ids)>1 else '',
                identity_evidence='No guard sidecar; single-object continuity only' if len(ids)==1 else 'Legacy multi-object identity not assessed')
            tracking_dimensions(row,guard,scale,scale_info)
            reasons=[]
            if not row['mask_valid']:reasons.extend(guard['reasons'] or ['tracking_contact_or_area_outlier'])
            if fit_quality is not None:
                item=fit_quality[frame]
                reasons.extend(item['quality_reasons'])
                row.update(mask_valid=row['mask_valid'] and item['mask_valid'],centerline_topology=item['topology'],
                           centerline_candidate=item['selected_candidate'],branch_score_margin=item['branch_score_margin'])
                if not item['mask_valid']:
                    reasons.append('fit_mask_rejected');row['mask_status']='rejected';row['mask_reason']+=';fit_mask_rejected'
                if not item.get('centerline_valid',not item['quality_reasons']):reasons.append('pose_solver_rejected')
                if 'uncertain_point_ranges' in item:
                    row.update(uncertain_point_ranges=json.dumps(item['uncertain_point_ranges']),
                               endpoint_a_inferred=item.get('endpoint_a_inferred',False),
                               endpoint_b_inferred=item.get('endpoint_b_inferred',False),
                               temporal_anchors=json.dumps(item.get('temporal_anchors',[])))
            try:
                if fitted is None:
                    if not row['mask_valid']:raise ValueError('mask_excluded')
                    p,r,qa=centerline(mask)
                else:
                    p=fitted[frame].copy()
                    if not np.isfinite(p).all(): raise ValueError('centerline_missing')
                    dt=ndimage.distance_transform_edt(mask)
                    r=ndimage.map_coordinates(dt,[p[:,1],p[:,0]],order=1,mode='constant')
                    inside=ndimage.map_coordinates(mask.astype(float),[p[:,1],p[:,0]],order=0,mode='constant')
                    qa=dict(centerline_inside_fraction=float(inside.mean()),**geometry_quality(p))
                    qa.pop('crossing_segments')
                    if qa['local_hairpin']:reasons.append('local_hairpin')
                center=p.mean(axis=0)
                if fitted is not None:
                    reverse=False  # Preserve extracted geometry; head order is resolved below.
                elif previous is None:
                    reverse=False
                else:
                    a=np.mean(np.linalg.norm((p-center)-(previous-previous_center),axis=1))
                    b=np.mean(np.linalg.norm((p[::-1]-center)-(previous-previous_center),axis=1))
                    reverse=b<a
                if reverse:p,r=p[::-1].copy(),r[::-1].copy()
                radius_profiles[frame]=r
                if qa.get('skeleton_path_fraction',1)<.7:reasons.append('branched_or_contact_skeleton')
                if qa['centerline_inside_fraction']<.97:reasons.append('centerline_outside_mask')
                length=float(np.linalg.norm(np.diff(p,axis=0),axis=1).sum()*scale)
                row.update(length_um=length,volume_estimate_um3=frustum_volume(p,r)*scale**3,
                           mean_width_um=float(2*r.mean()*scale),head_candidate_angle_deg=signed_angle(p),
                           opposite_end_angle_deg=signed_angle(p[::-1]),**qa)
                row['endpoint_a_angle_deg']=signed_angle(p)
                row['volume_valid']=not reasons and row.get('centerline_topology','simple')=='simple'
                if not row['volume_valid']:row['volume_estimate_um3']=np.nan
                if head_status=='unconfirmed_A_B_endpoints':row['head_candidate_angle_deg']=np.nan
                row['head_x_crop_px'],row['head_y_crop_px']=p[0]
                row['tail_x_crop_px'],row['tail_y_crop_px']=p[-1]
                heading=p[10]-p[30];heading/=max(np.linalg.norm(heading),1e-9)
                row['heading_x'],row['heading_y']=heading
                lines[(worm,frame)]=p
                if not reasons:previous,previous_center=p,center
            except (ValueError,IndexError) as exc:reasons.append(str(exc))
            row['centerline_valid']=not reasons
            row['valid_for_summary']=not reasons
            row['quality_reason']=';'.join(dict.fromkeys(reasons))
            rows.append(row)
        valid=np.array([r['centerline_valid'] for r in rows])
        lengths=np.array([r['length_um'] for r in rows]);median_length=pose_length_reference(rows)
        # Detect incomplete/merged masks even if the tracker never returned empty.
        for i in range(n):
            if valid[i] and np.isfinite(median_length) and not (.65*median_length<=lengths[i]<=1.35*median_length):
                valid[i]=False;rows[i]['quality_reason']+=';length_outlier';rows[i]['valid_for_summary']=False;rows[i]['centerline_valid']=False
                rows[i]['volume_valid']=False;rows[i]['volume_estimate_um3']=np.nan
        # Individual metrics need both local geometry and continuous identity.
        # Area may still be presented anonymously after separation.
        valid &= np.array([r['identity_valid'] and r['source_valid'] and (r['scale_valid'] or r.get('measurement_mode')=='pixels') for r in rows])
        for i,row in enumerate(rows):row['valid_for_summary']=bool(valid[i])
        median_length=finite_stat(lengths[valid])
        # Use only reliable simple postures to establish endpoint identity.
        poses=np.stack([lines.get((worm,i),np.full((101,2),np.nan)) for i in range(n)])
        head_valid=valid & np.array([r.get('centerline_topology','simple')=='simple'
            and not r.get('endpoint_a_inferred',False) and not r.get('endpoint_b_inferred',False) for r in rows])
        from head_appearance import extract_profiles
        intensity_profiles=extract_profiles(source,poses,radius_profiles,head_valid)
        heads=detect_head_tail(poses,head_valid,manual=manual,
            mode=video_config.get('head_detection','twardis'),parameters=config.get('head_detection_parameters'),
            segment_ids=[r['identity_segment'] for r in rows],radius_profiles=radius_profiles,intensity_profiles=intensity_profiles,fps=fps)
        for i,(row,head) in enumerate(zip(rows,heads)):
            row.update(head)
            row['head_metrics_valid']=bool(valid[i] and head['head_endpoint'] is not None and head['head_identity_confirmed'])
            row['head_candidate_angle_deg']=np.nan
            for key in ('head_x_crop_px','head_y_crop_px','tail_x_crop_px','tail_y_crop_px','heading_x','heading_y'): row[key]=np.nan
            p=lines.get((worm,i))
            if p is None: continue
            if head['head_endpoint']==1:
                p=p[::-1].copy();lines[(worm,i)]=p
                a,b=row.get('endpoint_a_inferred',False),row.get('endpoint_b_inferred',False)
                row['endpoint_a_inferred'],row['endpoint_b_inferred']=b,a
                if row.get('uncertain_point_ranges'):
                    row['uncertain_point_ranges']=json.dumps(sorted([len(p)-1-hi,len(p)-1-lo]
                        for lo,hi in json.loads(row['uncertain_point_ranges'])))
            row['endpoint_a_angle_deg']=signed_angle(p)
            row['opposite_end_angle_deg']=signed_angle(p[::-1])
            if row['head_metrics_valid']:
                row['head_candidate_angle_deg']=signed_angle(p)
                row['head_x_crop_px'],row['head_y_crop_px']=p[0]
                row['tail_x_crop_px'],row['tail_y_crop_px']=p[-1]
                heading=p[10]-p[30];heading/=max(np.linalg.norm(heading),1e-9)
                row['heading_x'],row['heading_y']=heading
        for row in rows:finish_dimensions(row)
        confirmed_frames=sum(r['head_identity_confirmed'] for r in rows)
        candidate_frames=sum(r['head_detection_status'] in CANDIDATE_STATUSES for r in rows)
        known_frames=confirmed_frames+candidate_frames
        head_status=('mixed_head_assignments' if confirmed_frames and candidate_frames else
                     'confirmed_by_user_point' if confirmed_frames else
                     'unconfirmed_taper_candidate' if any(r['head_detection_status']=='unconfirmed_taper_candidate' for r in rows) else
                     'unconfirmed_relative_motion_candidate' if candidate_frames else 'unconfirmed_A_B_endpoints')
        centers=np.array([[r['centroid_x_um'],r['centroid_y_um']] for r in rows])
        mask_valid=np.array([permissions(r)['motion'] for r in rows])
        window=max(5,int(round(config['speed_smoothing_s']*fps))|1)
        segments=np.array([r['identity_segment'] for r in rows])
        trajectory_runs=[idx for segment in np.unique(segments) for idx in runs(mask_valid & (segments==segment))]
        smoothed=np.full_like(centers,np.nan)
        for idx in trajectory_runs:
            local=smooth_valid(centers[idx],np.ones(len(idx),bool),window)
            smoothed[idx]=local
        velocity=np.full_like(smoothed,np.nan)
        for idx in trajectory_runs:
            if len(idx)>=2:velocity[idx]=np.gradient(smoothed[idx],times[idx],axis=0)
            if rows[idx[0]].get('identity_link_source') in ('auto_reassociated','human_same'):
                velocity[idx[0]]=np.nan
        speeds=np.linalg.norm(velocity,axis=1);bl_s=speeds/median_length
        angles=np.array([r['head_candidate_angle_deg'] for r in rows])
        angle_valid=valid&np.isfinite(angles)
        angle_smooth=np.full(n,np.nan);events=[];cycles=0;duration=0.
        # Never smooth or count a bend across a break in endpoint identity.
        for segment in sorted({r['head_segment'] for r in rows if r['head_metrics_valid']}):
            accepted=angle_valid & np.array([r['head_segment']==segment for r in rows])
            sm=smooth_valid(angles,accepted,5);angle_smooth[accepted]=sm[accepted]
            ev,cy,du=bend_events(sm,accepted,fps,config['head_angle_threshold_deg'],config['minimum_bend_interval_s'],times)
            events.extend(ev);cycles+=cy;duration+=du
        all_events.extend(dict(sample=sample,worm_id=worm,head_status=rows[e['frame']]['head_status'],**e) for e in events)
        circle=np.zeros(n,bool);circle_window=max(3,round(config['circling_window_s']*fps))
        for idx in trajectory_runs:
            for j in range(circle_window,len(idx)+1):
                sub=idx[j-circle_window:j]
                if is_circle_path(smoothed[sub],median_length,fps,config,times[sub]):
                    circle[sub]=True
        for i,row in enumerate(rows):
            row['speed_um_s']=float(speeds[i]);row['speed_bl_s']=float(bl_s[i])
            row['centroid_smoothed_x_um']=float(smoothed[i,0]);row['centroid_smoothed_y_um']=float(smoothed[i,1])
            row['head_candidate_angle_smoothed_deg']=float(angle_smooth[i])
            if not mask_valid[i] or not np.isfinite(speeds[i]) or not np.isfinite(bl_s[i]):state='unknown'
            elif bl_s[i]<config['stationary_speed_bl_s']:state='stationary'
            elif circle[i]:state='circling_candidate'
            elif not valid[i]:state='moving_posture_uncertain'
            elif not row['head_metrics_valid']:state='moving_head_unconfirmed'
            else:
                projection=float(np.dot(velocity[i],[row['heading_x'],row['heading_y']])/max(speeds[i],1e-9))
                state='forward_candidate' if projection>.5 else 'backward_candidate' if projection<-.5 else 'lateral_or_turning'
                if row['head_identity_confirmed']:state=state.replace('_candidate','')
            row['movement_state']=state
        good=smoothed[mask_valid];spread=float(np.linalg.norm(np.ptp(good,axis=0))/median_length) if len(good) else np.nan
        if len(trajectory_runs)>1:spread=np.nan
        mean_speed=finite_stat(bl_s,np.mean)
        if not np.isfinite(mean_speed) or not np.isfinite(spread):activity='unknown_insufficient_valid_data'
        elif mean_speed>=config['active_speed_bl_s'] and spread>=config['active_range_bl']:activity='A'
        elif mean_speed<config['stationary_speed_bl_s'] and spread<config['localized_range_bl']:
            activity='stationary_head_unconfirmed' if head_status=='unconfirmed_A_B_endpoints' else 'C' if events else 'stationary_without_detected_head_bends'
        else:activity='B'
        all_summaries.append(dict(sample=sample,worm_id=worm,frames=n,valid_frames=int(valid.sum()),
            quality_schema_version=QUALITY_VERSION,mask_valid_frames=sum(r['mask_valid'] for r in rows),
            identity_valid_frames=sum(r['identity_valid'] for r in rows),trajectory_valid_frames=int(mask_valid.sum()),
            identity_unverified_frames=sum(not r['identity_valid'] for r in rows),
            identity_status='review_required' if any(not r['identity_valid'] for r in rows) else 'provisional_or_reviewed_continuous',
            excluded_frames=np.flatnonzero(~valid).tolist(),head_status=head_status,head_taper_ratio=initial_width_ratio,
            head_detection_method=HEAD_TAIL_VERSION,head_known_frames=known_frames,
            head_confirmed_frames=confirmed_frames,head_candidate_frames=candidate_frames,
            head_unknown_frames=n-known_frames,head_manual_requested=bool(manual),
            head_evidence_score=finite_stat([r['head_evidence_score'] for r in rows if r['head_metrics_valid']]),
            head_reason_counts={reason:sum(r['head_reason']==reason for r in rows) for reason in sorted({r['head_reason'] for r in rows})},
            median_length_um=finite_stat(lengths[valid]),
            median_area_um2=finite_stat([rows[i]['area_um2'] for i in np.flatnonzero(mask_valid)]),
            median_volume_estimate_um3=finite_stat([rows[i]['volume_estimate_um3'] for i in np.flatnonzero(valid)]),
            mean_speed_um_s=finite_stat(speeds,np.mean),mean_speed_bl_s=mean_speed,
            path_length_um=float(sum(np.linalg.norm(np.diff(smoothed[idx],axis=0),axis=1).sum() for idx in trajectory_runs)) if np.isfinite(speeds).any() else np.nan,
            explored_bbox_diagonal_bl=spread,observed_valid_duration_s=duration,
            directional_bend_count=len(events) if angle_valid.any() else None,directional_bends_per_min=len(events)*60/duration if duration else None,
            observed_complete_cycles=cycles if angle_valid.any() else None,
            head_candidate_amplitude_deg=float(np.nanpercentile(np.abs(angle_smooth),95)) if angle_valid.any() else None,
            head_candidate_half_excursion_deg=float(np.subtract(*np.nanpercentile(angle_smooth,[95,5]))/2) if angle_valid.any() else None,
            activity_class_provisional=activity,
            movement_state_counts={state:sum(r['movement_state']==state for r in rows) for state in sorted({r['movement_state'] for r in rows})}))
        if source.get('measurement_mode') == 'pixels':
            from measurement_units import pixel_fields
            for row in rows: pixel_fields(row)
            pixel_fields(all_summaries[-1])
        from local_tracklets import measure as measure_tracklets
        tracklet_report=measure_tracklets(rows,fps,scale,timestamps=times)
        tracklet_reports.append(tracklet_report)
        all_summaries[-1].update(tracklet_pose_frames=sum(r['tracklet_pose_valid'] for r in rows),
            tracklet_additional_pose_frames=sum(r['tracklet_pose_valid'] and not r['identity_valid'] for r in rows))
        candidate_path=ROOT/'outputs'/sample/wormtracer_source/f'worm-{worm}'/'diagnostic-candidates.npy'
        candidates={}
        if candidate_path.exists():
            raw_candidates=np.load(candidate_path,allow_pickle=False)
            if raw_candidates.shape!=(n,101,2):raise ValueError('Display candidate shape mismatch')
            candidates={(worm,i):p for i,p in enumerate(raw_candidates) if np.isfinite(p).all()}
        display_lines.update(build_display(rows,lines,candidates,get_mask,centerline))
        all_summaries[-1].update(display_frames=sum(r['display_valid'] for r in rows),
            display_source_counts={k:sum(r['display_source']==k for r in rows) for k in sorted({r['display_source'] for r in rows})})
        diagnostics.extend(exclude_unusable(row) for row in rows)
        all_rows.extend(rows)
        print(f'{sample} worm {worm}: {valid.sum()}/{n} posture-valid, {mask_valid.sum()}/{n} mask-valid, head={head_status}',flush=True)
    from quality_audit import write_audit
    write_audit(output,all_rows)
    from local_tracklets import write as write_tracklets, VERSION as TRACKLET_VERSION
    tracklet_report=dict(tracklet_reports[0],tracklets=[t for report in tracklet_reports for t in report['tracklets']])
    write_tracklets(output,all_rows,tracklet_report)
    fields=list(dict.fromkeys(k for r in all_rows for k in r))
    with (output/'per_frame_metrics.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(json_safe(all_rows))
    for name,payload in [('per_frame_metrics.json',all_rows),('diagnostic-metrics.json',diagnostics),
                         ('quality.json',[dict(frame=r['frame'],worm_id=r['worm_id'],**describe(r)) for r in all_rows])]:
        (output/name).write_text(json.dumps(json_safe(payload),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    with (output/'bend_events.csv').open('w',newline='',encoding='utf-8-sig') as f:
        fields=['sample','worm_id','head_status','frame','time_s','direction','angle_deg']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(all_events)
    report=dict(sample=sample,mask_source=str(data),centerline_method=method,centerline_source=wormtracer_source if method!='skeleton' else 'skeleton',fps=fps,scale_um_per_px=None if source.get('measurement_mode')=='pixels' else scale,calibration=scale_info,
                measurement_mode=source.get('measurement_mode','physical'), metric_definition_version='ppt-20261021-v2',
                tracklet_version=TRACKLET_VERSION,tracklet_scope=tracklet_report['scope'],
                identity_reassociation_version=ASSOCIATION_VERSION,identity_reviews=video_config.get('identity_reviews',[]),
                timebase=timebase,
                config=config,worms=all_summaries,head_detection_version=HEAD_TAIL_VERSION,quality_schema_version=QUALITY_VERSION,display_version=DISPLAY_VERSION,
                identity_confirmations=video_config.get('identity_confirmations',[]),
                source_invalid_frames=video_config.get('source_invalid_frames',[]),
                head_detection_mode=video_config.get('head_detection','twardis'),
                head_annotations=video_config.get('head_annotations',{}),
                caveats=['Official head metrics require a manual head point within a reliable continuous segment; automatic candidates are not measurements.',
                         'Volume assumes circular cross sections and piecewise conical frusta.',
                         'Activity/circling thresholds are operational, not validated biological cutoffs.',
                         'Anonymous area needs source/mask/scale. Individual posture and trajectories additionally require identity continuity; summaries exclude unverified identities. Continuity is not biological ground truth.',
                         'body-relative endpoint-bending and image-appearance candidates use reliable continuous segments; ambiguous/contact frames remain A/B. Evidence scores are not probabilities. Volume excludes self-contact frames.',
                         'Speed is in the camera coordinate system; no stage drift correction.'])
    report=json_safe(report)
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    np.savez_compressed(output/'diagnostic-centerlines.npz',**{f'w{w}_f{f}':p for (w,f),p in lines.items()})
    lookup={(r['worm_id'],r['frame']):r for r in all_rows}
    lines={key:p for key,p in lines.items() if permissions(lookup[key])['posture']}
    np.savez_compressed(output/'centerlines.npz',**{f'w{w}_f{f}':p for (w,f),p in lines.items()})
    np.savez_compressed(output/'display-centerlines.npz',**{f'w{w}_f{f}':p for (w,f),p in display_lines.items()})
    render(sample,source,output,ids,all_rows,display_lines)
    write_report(output,report,all_rows)
    return report


def write_report(output,report,rows):
    """Unit-aware saved report and plots; only public, gated values are plotted."""
    import os
    os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'outputs'/'.matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from measurement_units import metric_key, pixel_mode
    unit='px' if pixel_mode(report) else 'um'
    key=lambda name:metric_key(name,report)
    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    for item in report['worms']:
        worm=item['worm_id']; data=[r for r in rows if r['worm_id']==worm]
        t=np.array([r['time_s'] for r in data])
        for ax,field,label in [(axes[0,0],key('length_um'),f'Centerline length ({unit})'),
                              (axes[0,1],key('speed_um_s'),f'Centroid speed ({unit}/s)'),
                              (axes[1,0],'head_candidate_angle_smoothed_deg','Confirmed head angle (deg)')]:
            ax.plot(t,[r.get(field,np.nan) for r in data],label=f'Worm {worm}',linewidth=1.3)
            ax.set(xlabel='Time (s)',ylabel=label)
        # NaN and identity boundaries break trajectories instead of joining gaps.
        xx=np.array([r.get(key('centroid_x_um'),np.nan) for r in data],float)
        yy=np.array([r.get(key('centroid_y_um'),np.nan) for r in data],float)
        segments=np.array([r.get('identity_segment',0) for r in data])
        for segment in np.unique(segments):
            x=xx.copy(); y=yy.copy(); x[segments!=segment]=np.nan; y[segments!=segment]=np.nan
            axes[1,1].plot(x,y,linewidth=1.3)
    axes[1,1].invert_yaxis(); axes[1,1].set(xlabel=f'Crop x ({unit})',ylabel=f'Crop y ({unit})',title='Centroid trajectories')
    axes[1,1].set_aspect('equal',adjustable='datalim')
    for ax in axes.ravel(): ax.grid(alpha=.2)
    fig.suptitle(report['sample']+' | gaps = excluded; head metrics require manual confirmation')
    fig.savefig(output/'phenotype-plots.png',dpi=160); plt.close(fig)
    simple=[{k:v for k,v in w.items() if not isinstance(v,(list,dict))} for w in report['worms']]
    with (output/'worm_summary.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(simple[0])); writer.writeheader(); writer.writerows(simple)
    scale_text='仅像素单位；未提供物理标定。' if pixel_mode(report) else f"像素比例：{report['scale_um_per_px']:.6f} μm/px。"
    text=[f"# {report['sample']} 表型分析",'',scale_text,
          '','|线虫|有效姿态帧/总帧|体长 '+unit+'|面积 '+unit+'²|体积估算 '+unit+'³|平均速度 '+unit+'/s|头摆 次/min|',
          '|---|---|---|---|---|---|---|']
    for w in report['worms']:
        def metric(name):
            v=w.get(key(name)); return '缺失' if v is None else f'{v:.2f}'
        text.append(f"|{w['worm_id']}|{w['valid_frames']}/{w['frames']}|{metric('median_length_um')}|{metric('median_area_um2')}|{metric('median_volume_estimate_um3')}|{metric('mean_speed_um_s')}|{metric('directional_bends_per_min')}|")
    text += ['', '头摆只统计人工确认且连续可靠的片段；缺失不是零。单向弯曲记一次，频率分母为有效连续观察秒数。',
             '头部角度采用中线0–10%与10–30%方向夹角；幅度为绝对角度P95，另报分位半跨度。阈值为工程口径，尚需独立人工事件验证。',
             '体积采用圆形截面分段圆台估算，不是实测三维体积。速度位于图像坐标系，未校正载物台漂移。',
             'A/B/C与转圈为待验证研究规则；本轮不作为正式表型结论。',
             '', '[逐帧数据](per_frame_metrics.csv) · [单虫汇总](worm_summary.csv) · [摆动事件](bend_events.csv) · [质量](quality.json)',
             '', '![表型曲线](phenotype-plots.png)']
    (output/'报告.md').write_text('\n'.join(text),encoding='utf-8')


def render(sample,source,output,ids,rows,lines):
    _,_,_,get_mask,_=load_sample(sample,source.get('mask_source'))
    cap=cv2.VideoCapture(source['video']);fps=source['fps'];x0,y0,x1,y1=source['crop_xyxy']
    lookup={(r['worm_id'],r['frame']):r for r in rows};colors={w:worm_color(w) for w in ids}
    from result_overlay import draw_worm, draw_hud
    temp=Path(tempfile.gettempdir())/f'phenotypes-{sample}-{output.name}.mp4'
    width,height=x1-x0,y1-y0
    writer=cv2.VideoWriter(str(temp),cv2.VideoWriter_fourcc(*'mp4v'),fps,(width+width%2,height+height%2))
    if not writer.isOpened():raise RuntimeError('Cannot create phenotype video')
    for frame in range(source['frames']):
        report_progress(frame,source['frames'],'绘制绿色轮廓、白色中线与逐帧指标',fraction=.6+.39*frame/source['frames'])
        ok,image=cap.read()
        if not ok:raise RuntimeError('Truncated source video')
        image=image[y0:y1,x0:x1].copy()
        current_rows = [lookup[(worm,frame)] for worm in ids]
        for worm, row in zip(ids,current_rows):
            draw_worm(image,get_mask(worm,frame),lines.get((worm,frame)),row)
        draw_hud(image,current_rows,frame,fps)
        report_preview(image,frame,source['frames'],'表型叠加画面 · 质量状态以结果页为准')
        writer.write(cv2.copyMakeBorder(image,0,height%2,0,width%2,cv2.BORDER_CONSTANT))
        if frame in (0,source['frames']//2,source['frames']-1):write_png(output/f'preview-{frame:04d}.png',image)
    writer.release();cap.release();shutil.copy2(temp,output/'phenotypes-overlay.mp4');temp.unlink()
    check=cv2.VideoCapture(str(output/'phenotypes-overlay.mp4'))
    count=int(check.get(cv2.CAP_PROP_FRAME_COUNT));check.release()
    if count!=source['frames']:raise RuntimeError('Export video frame count mismatch')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample',default='all',help='Sample directory name; all retains the two original samples')
    parser.add_argument('--config',type=Path,default=ROOT/'phenotype_config.json')
    parser.add_argument('--centerline-method',choices=['skeleton','wormtracer','niu','hybrid','topology','tapnext'],default='wormtracer')
    parser.add_argument('--wormtracer-source',default=None,help='Default: contact correction if present, otherwise temporal')
    parser.add_argument('--render-only',action='store_true',help='Refresh videos and previews using saved measurements')
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    for sample in (['2-1-1314','5-1-910'] if args.sample=='all' else [args.sample]):
        from centerline_methods import method_source
        default_source=method_source(args.centerline_method) if args.centerline_method in ('niu','hybrid','topology','tapnext') else None
        fit_source=args.wormtracer_source or default_source or ('wormtracer-contact-corrected' if (ROOT/'outputs'/sample/'wormtracer-contact-corrected'/'mask-source.json').exists() else 'wormtracer-temporal')
        if args.render_only:
            _,source,ids,_,_=load_sample(sample,fitted_mask_source(sample,fit_source) if args.centerline_method!='skeleton' else None)
            output=ROOT/'outputs'/sample/('phenotypes-'+fit_source if args.centerline_method!='skeleton' else 'phenotypes')
            with (output/'per_frame_metrics.csv').open(encoding='utf-8-sig') as handle:
                rows=list(csv.DictReader(handle))
            for row in rows:
                for key in ('worm_id','frame'):row[key]=int(row[key])
                for key in ('length_um','speed_um_s'):row[key]=float(row[key]) if row[key] else np.nan
                for key in ('valid_for_summary','centerline_valid','mask_valid','head_identity_confirmed','volume_valid','endpoint_a_inferred','endpoint_b_inferred'):
                    if key in row:row[key]=row[key].lower()=='true'
            with np.load(output/('display-centerlines.npz' if (output/'display-centerlines.npz').exists() else 'centerlines.npz')) as saved:
                lines={tuple(int(v[1:]) for v in key.split('_')):saved[key] for key in saved.files}
            render(sample,source,output,ids,rows,lines)
            print(f'{sample}: refreshed contour, centerline and per-frame measurements',flush=True)
        else:analyze(sample,config,args.centerline_method,fit_source)


if __name__=='__main__':main()
