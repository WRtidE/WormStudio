"""Measure PPT-defined phenotypes from saved SAM2 masks, without re-segmenting.

Run in the project environment: uv run --no-sync src/analyze_phenotypes.py --sample all
Default: latest saved contact correction, or temporal WormTracer centerlines.
Each corrected fit selects its matching mask source through mask-source.json.
Use --centerline-method skeleton to reproduce the original baseline.
Head coordinates in phenotype_config.json: {"2-1-1314:1": [x, y], ...}.
Coordinates are within the saved cropped first frame, not the full video.
"""
import argparse
from pipeline_progress import report_progress
from live_preview import enabled as preview_enabled, report_preview
import csv
import json
import tempfile
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage
from scipy.signal import find_peaks, savgol_filter
from centerline_topology import geometry_quality
from result_overlay import OVERLAY_VERSION, draw_worm
from head_tail import detect_head_tail, CANDIDATE_STATUSES, VERSION as HEAD_TAIL_VERSION
from head_bend_geometry import local_angle, supported_angles, turn
from image_io import write_png
from serialization import json_safe, write_json_records
from skeleton_geometry import centerline, frustum_volume
from tracking_data import load_sample
from display_centerlines import build_display, track_visible_tips, VERSION as DISPLAY_VERSION
from phenotype_statistics import VERSION as METRIC_VERSION, measure_posture, trajectory_statistics, behavior_statistics
from quality_schema import VERSION as QUALITY_VERSION, tracking_dimensions, finish_dimensions, permissions, exclude_unusable, describe

from resource_paths import PROJECT_ROOT as ROOT
# 导出视频中的中心线宽度（原来为 3 像素）；可在这里调整。
CENTERLINE_WIDTH_PX = 2


def finite_stat(values, statistic=np.median):
    data=np.asarray(values,dtype=float)
    data=data[np.isfinite(data)]
    return float(statistic(data)) if len(data) else np.nan


def signed_angle(points):
    # Head-outward tangent (0-10%) relative to the neck-outward tangent (10-30%).
    a=points[0]-points[10]; b=points[10]-points[30]
    return turn(a,b)


def runs(valid,segments=None):
    indices=np.flatnonzero(valid)
    if not len(indices):return []
    cut=np.diff(indices)>1
    if segments is not None:cut |= segments[indices[1:]]!=segments[indices[:-1]]
    return np.split(indices,np.flatnonzero(cut)+1)


def smooth_valid(values,valid,window,*,angles=False,segments=None):
    result=np.full_like(values,np.nan,dtype=float)
    for idx in runs(valid,segments):
        w=min(window,len(idx) if len(idx)%2 else len(idx)-1)
        part=values[idx]
        if angles:part=np.degrees(np.unwrap(np.radians(part)))
        smoothed=savgol_filter(part,w,2,axis=0) if w>=5 else part
        result[idx]=(smoothed+180)%360-180 if angles else smoothed
    return result


def bend_events(angles,valid,fps,threshold,min_interval,timestamps=None,*,segments=None):
    if timestamps is None:timestamps=np.arange(len(valid))/fps
    events=[]; cycles=0; duration=0.
    distance=max(1,int(round(min_interval*fps)))
    for idx in runs(valid,segments):
        if len(idx)<3:continue
        duration+=timestamps[idx[-1]]-timestamps[idx[0]]
        signal=np.degrees(np.unwrap(np.radians(angles[idx])))
        signal-=np.median(signal)
        candidates=[]
        for sign in (1,-1):
            side=sign*signal
            peaks=find_peaks(side,prominence=threshold/2,distance=distance)[0]
            for i in peaks:
                left,peak,right=side[i-1:i+2]
                curvature=left+right-2*peak
                # Estimate the peak height between samples, keeping its observed frame and time.
                height=peak-(right-left)**2/(8*curvature) if curvature<0 else peak
                if height>=threshold:
                    candidates.append((int(i),sign,float(height)))
        selected=[]
        for i,sign,height in sorted(candidates):
            if selected and selected[-1][1]==sign and np.min(sign*signal[selected[-1][0]:i+1])>threshold/4:
                if height>selected[-1][2]:selected[-1]=(i,sign,height)
            else:selected.append((i,sign,height))
        alternating=[]
        for i,sign,height in selected:
            if alternating and alternating[-1][1]==sign:alternating.clear()
            alternating.append((i,sign,height))
            event=dict(frame=int(idx[i]),time_s=float(timestamps[idx[i]]),direction=sign,
                       angle_deg=float(angles[idx[i]]),excursion_deg=sign*height)
            if len(alternating)==3:
                # Two peak-to-trough half-swings between consecutive same-side peaks.
                first,middle,last=(v for _,_,v in alternating)
                event['cycle_amplitude_deg']=(first+2*middle+last)/4
                event['cycle_period_s']=float(timestamps[idx[i]]-timestamps[idx[alternating[0][0]]])
                cycles+=1
                alternating=alternating[-1:]
            events.append(event)
    return events,cycles,duration


def trajectory_kinematics(points,times,segments,window,rows):
    smoothed=np.full_like(points,np.nan)
    velocity=np.full_like(points,np.nan)
    for idx in segments:
        part=points[idx]
        smoothed[idx]=part if np.all(part==part[0]) else smooth_valid(part,np.ones(len(idx),bool),window)
        if len(idx)>=2:
            velocity[idx]=np.gradient(smoothed[idx],times[idx],axis=0)
        if rows[idx[0]].get('identity_link_source') in ('auto_reassociated','human_same'):
            velocity[idx[0]]=np.nan
    return smoothed,velocity


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


def circle_evidence(path,times,segments,body_length,fps,config):
    observed=np.zeros(len(path),bool)
    candidate=np.zeros(len(path),bool)
    if not np.isfinite(body_length) or body_length<=0:return observed,candidate
    for idx in segments:
        t=times[idx]
        for factor in (1,2):
            seconds=factor*config['circling_window_s']
            starts=np.searchsorted(t,t-seconds+1e-9,side='right')-1
            for end in np.flatnonzero(starts>=0):
                sub=idx[starts[end]:end+1]
                if len(sub)<3:continue
                observed[sub]=True
                if is_circle_path(path[sub],body_length,fps,config,times[sub]):candidate[sub]=True
    return observed,candidate


def pose_length_reference(rows):
    """Keep the geometry gate anchored to pre-association continuity evidence."""
    return finite_stat([r['length_um'] for r in rows if r['centerline_valid']
        and r.get('identity_original_valid',r['identity_valid'])])


def analyze(sample,config,method='skeleton',wormtracer_source='wormtracer',*,defer_video=False):
    data,source,ids,get_mask,bad=load_sample(sample,fitted_mask_source(sample,wormtracer_source) if method!='skeleton' else None,root=ROOT)
    output=ROOT/'outputs'/sample/('phenotypes-'+wormtracer_source if method!='skeleton' else 'phenotypes');output.mkdir(parents=True,exist_ok=True)
    scale,scale_info=calibration(sample,source,config)
    fps=source['fps'];n=source['frames'];all_rows=[];all_summaries=[];all_events=[];lines={}
    from video_timing import verify_timebase
    timebase=verify_timebase(source['video'],n,fps)
    fps=timebase['fps']
    times=np.asarray(timebase['timestamps_s'])
    snapshot=ROOT/'outputs'/sample/'pipeline-config.json'
    video_config=json.loads(snapshot.read_text(encoding='utf-8')) if snapshot.is_file() else {}
    from tracking_data import load_guards
    guards=load_guards(data,ids,n,required=bool(video_config.get('quality_schema_version')))
    tracking_guards = guards
    from identity_reassociation import reassociate,VERSION as ASSOCIATION_VERSION,RULES
    association_report=dict(version=ASSOCIATION_VERSION,rules=RULES,edges=[],scope='legacy guards unavailable')
    if method == 'self_contact':
        # Use the very same identity boundaries and reviewed jumps that constrained the fit.
        for worm in ids:
            fit_dir = ROOT/'outputs'/sample/wormtracer_source/f'worm-{worm}'
            if guards is None:
                raise ValueError('Missing mask quality guards for the self-contact fit')
            records = json.loads((fit_dir/'identity-guards.json').read_text(encoding='utf-8'))
            if len(records) != n or any(r['frame'] != f or r['worm_id'] != worm for f,r in enumerate(records)):
                raise ValueError('Identity decisions do not match this track')
            guards[worm] = records
            if worm == ids[0]:
                fit_report = json.loads((fit_dir/'events.json').read_text(encoding='utf-8'))
                association_report = fit_report.get('identity_reassociation', dict(
                    version=ASSOCIATION_VERSION, rules=RULES, edges=[], scope='original tracking guards'))
    elif guards is not None:
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
        radius_profiles=None if method=='body_sequence' else np.full((n,101),np.nan)
        head_point=config.get('head_points_crop_xy',{}).get(f'{sample}:{worm}')
        manual=video_config.get('head_annotations',{}).get(str(worm))
        if manual is None and head_point is not None: manual=dict(frame=0,point=head_point)
        if manual is not None and 'crop_xyxy' in source:
            from head_evidence import anchors_in_crop
            manual=anchors_in_crop(manual,video_config.get('crop_xyxy',source['crop_xyxy']),source['crop_xyxy'])
        head_status='unconfirmed_A_B_endpoints'
        initial_width_ratio=None
        for frame in range(n):
            done=ids.index(worm)*n+frame
            report_progress(done,n*len(ids),f'计算线虫 {worm} 的逐帧表型',unit='虫帧',fraction=.6*done/(n*len(ids)))
            mask=get_mask(worm,frame);ys,xs=np.nonzero(mask)
            row={'sample':sample,'worm_id':worm,'frame':frame,'time_s':timebase['timestamps_s'][frame],
                 'mask_bbox_crop_xyxy':json.dumps([int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)]) if len(xs) else '',
                 'time_valid':True,'time_status':timebase['status'],
                 'body_center_x_source_px':np.nan,'body_center_y_source_px':np.nan,'centroid_x_source_px':float(xs.mean()+source['crop_xyxy'][0]) if len(xs) else np.nan,
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
                if method == 'self_contact':
                    row['self_contact_state'] = item['selected_candidate']
                if item.get('contact_bridge'):
                    row['contact_bridge']=json.dumps(item['contact_bridge'])
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
                    if method == 'self_contact':
                        x, y, w, h = cv2.boundingRect(mask.astype(np.uint8))
                        dt = ndimage.distance_transform_edt(np.pad(mask[y:y+h,x:x+w],2))
                        coords = (p - [x-2,y-2]).T[::-1]
                    else:
                        dt = ndimage.distance_transform_edt(mask)
                        coords = p.T[::-1]
                    r=ndimage.map_coordinates(dt,coords,order=1,mode='constant')
                    inside=ndimage.map_coordinates(mask.astype(float),[p[:,1],p[:,0]],order=0,mode='constant')
                    qa=dict(centerline_inside_fraction=float(inside.mean()),**geometry_quality(p))
                    qa.pop('crossing_segments')
                    if method != 'self_contact' and qa['local_hairpin']:reasons.append('local_hairpin')
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
                if radius_profiles is not None:radius_profiles[frame]=r
                if qa.get('skeleton_path_fraction',1)<.7:reasons.append('branched_or_contact_skeleton')
                if method != 'self_contact' and qa['centerline_inside_fraction']<.97:reasons.append('centerline_outside_mask')
                if method=='body_sequence':
                    from framewise_pose import arc_midpoint
                    mid=arc_midpoint(p)
                    if mid is not None:
                        mx,my=np.rint(mid).astype(int)
                        if 0<=my<mask.shape[0] and 0<=mx<mask.shape[1] and mask[my,mx]:
                            row['body_center_x_source_px'],row['body_center_y_source_px']=mid+source['crop_xyxy'][:2]
                length=float(np.linalg.norm(np.diff(p,axis=0),axis=1).sum()*scale)
                row.update(length_um=length,volume_estimate_um3=frustum_volume(p,r)*scale**3,
                           mean_width_um=float(2*r.mean()*scale),head_candidate_angle_deg=signed_angle(p),
                           opposite_end_angle_deg=signed_angle(p[::-1]),**qa)
                row['endpoint_a_angle_deg']=signed_angle(p)
                if row.get('contact_bridge'):
                    row['mean_width_um']=np.nan  # Overlapping silhouettes inflate the distance-transform width.
                if method=='body_sequence':
                    if row.get('endpoint_a_inferred') or row.get('endpoint_b_inferred'):
                        row['length_estimate_um']=row['length_um']
                        row['length_um']=np.nan
                        row['length_measurement_status']='hidden_endpoint_estimate'
                    if row.get('centerline_topology')!='simple':
                        row['mean_width_um']=np.nan
                        row['width_measurement_status']='contact_width_unresolved'
                row['volume_valid']=not reasons and row.get('centerline_topology','simple')=='simple'
                if method == 'self_contact' and (qa.get('local_hairpin') or qa['centerline_inside_fraction'] < .97):
                    row['volume_valid'] = False
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
            if (method != 'self_contact' and valid[i] and np.isfinite(lengths[i]) and np.isfinite(median_length)
                    and not (.65*median_length<=lengths[i]<=1.35*median_length)):
                valid[i]=False;rows[i]['quality_reason']+=';length_outlier';rows[i]['valid_for_summary']=False;rows[i]['centerline_valid']=False
                rows[i]['volume_valid']=False;rows[i]['volume_estimate_um3']=np.nan
        # Individual metrics need both local geometry and continuous identity.
        # Area may still be presented anonymously after separation.
        valid &= np.array([r['identity_valid'] and r['source_valid'] and (r['scale_valid'] or r.get('measurement_mode')=='pixels') for r in rows])
        for i,row in enumerate(rows):row['valid_for_summary']=bool(valid[i])
        median_length=finite_stat(lengths[valid])
        seed_mode = video_config.get('head_detection','twardis') in ('taper_seed','manual_seed')
        if method=='body_sequence':
            from body_sequence_solver import head_results
            heads=head_results(fit_quality)
        else:
            # Archived/baseline callers still compute their own endpoint order.
            # The current solver already saved that evidence; do not decode the
            # video a second time for appearance profiles it will never consume.
            poses=np.stack([lines.get((worm,i),np.full((101,2),np.nan)) for i in range(n)])
            head_valid=valid & np.array([r.get('centerline_topology','simple')=='simple'
                and not r.get('endpoint_a_inferred',False) and not r.get('endpoint_b_inferred',False) for r in rows])
            endpoint_visible = None
            if method == 'self_contact':
                head_valid = valid
                if fit_quality is not None:
                    endpoint_visible = [[r.get('endpoint_a_inferred') is False,
                                         r.get('endpoint_b_inferred') is False] for r in fit_quality]
            from head_appearance import extract_profiles
            intensity_profiles = (None if method == 'self_contact' and seed_mode else
                                  extract_profiles(source,poses,radius_profiles,head_valid))
            heads=detect_head_tail(poses,head_valid,manual=manual,
                mode=video_config.get('head_detection','twardis'),parameters=config.get('head_detection_parameters'),
                segment_ids=[r['identity_segment'] for r in rows],radius_profiles=radius_profiles,intensity_profiles=intensity_profiles,
                fps=fps,endpoint_visible=endpoint_visible,
                pose_gap_allowed=[not r['centerline_valid'] and r['source_valid'] and r['mask_valid'] and r['identity_valid']
                                  for r in rows] if endpoint_visible is not None else None)
            from head_temporal import apply_contact_directions
            apply_contact_directions(heads,rows,fit_quality,video_config.get('head_detection','twardis'))
        candidate_path=ROOT/'outputs'/sample/wormtracer_source/f'worm-{worm}'/'diagnostic-candidates.npy'
        candidates={}
        if candidate_path.exists():
            raw_candidates=np.load(candidate_path,allow_pickle=False)
            if raw_candidates.shape!=(n,101,2):raise ValueError('Display candidate shape mismatch')
            candidates={(worm,i):p
                        for i,p in enumerate(raw_candidates) if np.isfinite(p).all()}
        # Display geometry is still in its native point order here.
        for row in rows:
            row['pose_valid'] = row['centerline_valid']
        visible = build_display(rows,lines,candidates,get_mask,centerline,allow_fallback=method!='body_sequence')
        if method == 'self_contact' and fit_quality and video_config.get('head_detection') != 'manual_only':
            from head_temporal import orient_loop_reviews
            orient_loop_reviews(rows, heads, fit_quality, visible, worm)
        mode = video_config.get('head_detection','twardis')
        if seed_mode and method not in ('self_contact','body_sequence'):
            from endpoint_order import order_endpoints
            order_endpoints(rows, heads, visible, worm, fps, manual, len(ids)==1,
                            intensity_profiles, config.get('head_detection_parameters'))
        for i,row in enumerate(rows):
            key = (worm,i)
            if key not in visible:
                continue
            reverse = row.get('endpoint_order_reversed',heads[i]['head_endpoint']==1)
            display_lines[key] = visible[key][::-1].copy() if reverse else visible[key]
        for i,(row,head) in enumerate(zip(rows,heads)):
            row.update(head)
            row['head_metrics_valid']=bool(valid[i] and head['head_endpoint'] is not None and head['head_identity_confirmed']
                                           and not head.get('head_anchor_conflict'))
            row['head_candidate_angle_deg']=np.nan
            for key in ('head_x_crop_px','head_y_crop_px','tail_x_crop_px','tail_y_crop_px','heading_x','heading_y'): row[key]=np.nan
            p=lines.get((worm,i))
            if p is None: continue
            reverse = head['head_endpoint']==1
            if row['display_source'] in ('formal','geometry_review'):
                reverse = row.get('endpoint_order_reversed', reverse)
            if reverse:
                p=p[::-1].copy();lines[(worm,i)]=p
                a,b=row.get('endpoint_a_inferred',False),row.get('endpoint_b_inferred',False)
                row['endpoint_a_inferred'],row['endpoint_b_inferred']=b,a
                if row.get('uncertain_point_ranges'):
                    row['uncertain_point_ranges']=json.dumps(sorted([len(p)-1-hi,len(p)-1-lo]
                        for lo,hi in json.loads(row['uncertain_point_ranges'])))
            row['endpoint_a_angle_deg']=signed_angle(p)
            row['opposite_end_angle_deg']=signed_angle(p[::-1])
            if method=='body_sequence' and head['body_sequence_head_known'] and valid[i]:
                for side,point,inferred in (('head',p[0],'endpoint_a_inferred'),('tail',p[-1],'endpoint_b_inferred')):
                    if not row.get(inferred):
                        row[f'track_{side}_x_px'],row[f'track_{side}_y_px']=point
            if row['head_metrics_valid']:
                row['head_candidate_angle_deg']=signed_angle(p)
                row['head_x_crop_px'],row['head_y_crop_px']=p[0]
                row['tail_x_crop_px'],row['tail_y_crop_px']=p[-1]
                heading=p[10]-p[30];heading/=max(np.linalg.norm(heading),1e-9)
                row['heading_x'],row['heading_y']=heading
        if method == 'body_sequence':
            track_visible_tips(rows, display_lines, get_mask, fps)
        for i, row in enumerate(rows):
            finish_dimensions(row)
            measure_posture(row, lines.get((worm, i)), scale)
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
        if method=='body_sequence':
            breaks=np.array([r['body_sequence_break_before'] for r in rows])
            segments=np.cumsum(np.r_[True,np.diff(segments)!=0] | breaks)
        trajectory_runs=runs(mask_valid,segments)
        smoothed,velocity=trajectory_kinematics(centers,times,trajectory_runs,window,rows)
        trunk=np.full_like(centers,np.nan)
        for i,row in enumerate(rows):
            if valid[i] and row.get('centerline_topology','simple')=='simple' and not row.get('contact_bridge'):
                ranges=json.loads(row['uncertain_point_ranges']) if row.get('uncertain_point_ranges') else []
                if any(lo<=70 and hi>=30 for lo,hi in ranges):continue
                trunk[i]=lines[(worm,i)][30:71].mean(axis=0)*scale
        head_segments=np.array([row['head_segment'] for row in rows])
        head_blocks=np.cumsum(np.r_[True,(segments[1:]!=segments[:-1]) | (head_segments[1:]!=head_segments[:-1])])
        trunk_runs=runs(mask_valid & np.isfinite(trunk[:,0]),head_blocks)
        _,trunk_velocity=trajectory_kinematics(trunk,times,trunk_runs,window,rows)
        trunk_speeds=np.linalg.norm(trunk_velocity,axis=1)
        speeds=np.linalg.norm(velocity,axis=1);bl_s=speeds/median_length
        angles=np.array([r['head_candidate_angle_deg'] for r in rows])
        allowed_head=valid & np.array([permissions(r)['head'] for r in rows])
        angles[~allowed_head]=np.nan
        if method=='body_sequence' and len(ids)==1:
            evidence=np.full((n,4),np.nan)
            for i in np.flatnonzero(np.isfinite(angles)):
                row=rows[i]
                ranges=json.loads(row['uncertain_point_ranges']) if row.get('uncertain_point_ranges') else []
                if (row.get('endpoint_a_inferred') or row.get('centerline_topology','simple')!='simple'
                        or row.get('contact_bridge') or any(lo<=30 and hi>=0 for lo,hi in ranges)):
                    angles[i]=np.nan
                    continue
                geometry=local_angle(lines[(worm,i)],get_mask(worm,i))
                if geometry is not None:evidence[i]=geometry
            angles,angle_methods=supported_angles(angles,evidence,head_blocks,times)
            for row,angle,angle_method,block in zip(rows,angles,angle_methods,head_blocks):
                row['head_candidate_angle_deg']=float(angle)
                row['head_angle_method']=angle_method
                row['head_angle_block']=int(block)
        angle_valid=np.isfinite(angles)
        angle_smooth=smooth_valid(angles,angle_valid,5,angles=True,segments=head_blocks)
        events,cycles,duration=bend_events(angles,angle_valid,fps,config['head_angle_threshold_deg'],
                                           config['minimum_bend_interval_s'],times,segments=head_blocks)
        all_events.extend(dict(sample=sample,worm_id=worm,head_status=rows[e['frame']]['head_status'],**e) for e in events)
        circle_observed,circle=circle_evidence(smoothed,times,trajectory_runs,median_length,fps,config)
        for i,row in enumerate(rows):
            row['speed_um_s']=float(speeds[i]);row['speed_bl_s']=float(bl_s[i])
            row['centroid_smoothed_x_um']=float(smoothed[i,0]);row['centroid_smoothed_y_um']=float(smoothed[i,1])
            row['head_candidate_angle_smoothed_deg']=float(angle_smooth[i])
            row['circling_candidate']=bool(circle[i]) if circle_observed[i] else None
            if not mask_valid[i] or not np.isfinite(speeds[i]) or not np.isfinite(bl_s[i]):state='unknown'
            elif bl_s[i]<config['stationary_speed_bl_s']:state='stationary'
            elif not valid[i] or not np.isfinite(trunk_speeds[i]):state='moving_posture_uncertain'
            elif trunk_speeds[i]<config['stationary_speed_bl_s']*median_length:state='trunk_stationary'
            elif not row['head_metrics_valid']:state='moving_head_unconfirmed'
            else:
                projection=float(np.dot(trunk_velocity[i],[row['heading_x'],row['heading_y']])/max(trunk_speeds[i],1e-9))
                state='forward' if projection>.5 else 'backward' if projection<-.5 else 'lateral_or_turning'
            row['movement_state']=state
        good=smoothed[mask_valid];spread=float(np.linalg.norm(np.ptp(good,axis=0))/median_length) if len(good) else np.nan
        if len(trajectory_runs)>1:spread=np.nan
        mean_speed=finite_stat(bl_s,np.mean)
        states={row['movement_state'] for row in rows}
        stationary_observation=states.issubset({'stationary','trunk_stationary'})
        if not np.isfinite(mean_speed) or not np.isfinite(spread):activity='unknown_insufficient_valid_data'
        elif mean_speed>=config['active_speed_bl_s'] and spread>=config['active_range_bl']:activity='A'
        elif stationary_observation and spread<config['localized_range_bl']:
            activity='stationary_head_unconfirmed' if not duration else 'C' if events else 'stationary_without_detected_head_bends'
        elif mean_speed<config['active_speed_bl_s'] and spread<config['active_range_bl']:activity='B'
        else:activity='mixed_speed_range'
        cycle_seconds=sum(e['cycle_period_s'] for e in events if 'cycle_period_s' in e)
        all_summaries.append(dict(sample=sample,worm_id=worm,frames=n,valid_frames=int(valid.sum()),
            quality_schema_version=QUALITY_VERSION,mask_valid_frames=sum(r['mask_valid'] for r in rows),
            identity_valid_frames=sum(r['identity_valid'] for r in rows),trajectory_valid_frames=int(mask_valid.sum()),
            identity_unverified_frames=sum(not r['identity_valid'] for r in rows),
            identity_status='review_required' if any(not r['identity_valid'] for r in rows) else 'provisional_or_reviewed_continuous',
            excluded_frames=np.flatnonzero(~valid).tolist(),head_status=head_status,head_taper_ratio=initial_width_ratio,
            head_detection_method=HEAD_TAIL_VERSION,head_known_frames=known_frames,
            head_confirmed_frames=confirmed_frames,head_candidate_frames=candidate_frames,
            head_recovered_from_mark_frames=sum(r['head_detection_status']=='recovered_from_user_point' for r in rows),
            head_unknown_frames=n-known_frames,head_manual_requested=bool(manual),
            head_evidence_score=finite_stat([r['head_evidence_score'] for r in rows if r['head_metrics_valid']]),
            head_reason_counts={reason:sum(r['head_reason']==reason for r in rows) for reason in sorted({r['head_reason'] for r in rows})},
            median_length_um=finite_stat(lengths[valid]),
            median_area_um2=finite_stat([rows[i]['area_um2'] for i in np.flatnonzero(mask_valid)]),
            median_volume_estimate_um3=finite_stat([rows[i]['volume_estimate_um3'] for i in np.flatnonzero(valid)]),
            mean_speed_um_s=finite_stat(speeds,np.mean),mean_speed_bl_s=mean_speed,
            **trajectory_statistics(smoothed, times, trajectory_runs),
            **behavior_statistics(rows, times, trajectory_runs),
            normalized_speed_valid_frames=int(np.isfinite(bl_s).sum()),
            length_valid_frames=int(np.isfinite(lengths[valid]).sum()),
            area_valid_frames=int(sum(np.isfinite(rows[i]['area_um2']) for i in np.flatnonzero(mask_valid))),
            volume_valid_frames=int(sum(np.isfinite(rows[i]['volume_estimate_um3']) for i in np.flatnonzero(valid))),
            speed_valid_frames=int(np.isfinite(speeds).sum()),
            head_angle_valid_frames=int(np.isfinite(angle_smooth).sum()),
            explored_bbox_diagonal_bl=spread,observed_valid_duration_s=duration,
            directional_bend_count=len(events) if duration else None,directional_bends_per_min=len(events)*60/duration if duration else None,
            observed_complete_cycles=cycles if duration else None,
            head_complete_cycles_duration_s=cycle_seconds if duration else None,
            head_cycle_frequency_hz=cycles/cycle_seconds if cycles else None,
            head_cycle_amplitude_deg=finite_stat([e['cycle_amplitude_deg'] for e in events if 'cycle_amplitude_deg' in e]),
            head_candidate_amplitude_deg=float(np.nanpercentile(np.abs(angle_smooth),95)) if angle_valid.any() else None,
            head_candidate_half_excursion_deg=float(np.subtract(*np.nanpercentile(angle_smooth,[95,5]))/2) if angle_valid.any() else None,
            activity_class_provisional=activity,
            circling_observed_frames=int(circle_observed.sum()),circling_candidate_frames=int(circle.sum()) if circle_observed.any() else None,
            movement_state_counts={state:sum(r['movement_state']==state for r in rows) for state in sorted(states)}))
        if method=='body_sequence' and len(ids)==1:
            all_summaries[-1].update(head_angle_section_fit_frames=int(np.sum(angle_methods=='section_fit')),
                                     head_angle_review_frames=int(np.sum(angle_methods=='review_geometry')))
        for field, summary_key, count_key in (
                ('mean_width_um', 'median_width_um', 'width_valid_frames'),
                ('length_width_ratio', 'median_length_width_ratio', 'ratio_valid_frames'),
                ('body_extension', 'median_body_extension', 'extension_valid_frames'),
                ('midbody_abs_curvature_per_um', 'median_midbody_abs_curvature_per_um', 'curvature_valid_frames')):
            values = [row[field] for row in rows if row['valid_for_summary']
                      and (field != 'mean_width_um' or row.get('centerline_topology', 'simple') == 'simple')
                      and np.isfinite(row[field])]
            all_summaries[-1][summary_key] = finite_stat(values)
            all_summaries[-1][count_key] = len(values)
            if field == 'mean_width_um':
                all_summaries[-1]['mean_width_um'] = finite_stat(values, np.mean)
        if source.get('measurement_mode') == 'pixels':
            from measurement_units import pixel_fields
            for row in rows: pixel_fields(row)
            pixel_fields(all_summaries[-1])
        from local_tracklets import measure as measure_tracklets
        tracklet_report=measure_tracklets(rows,fps,scale,timestamps=times)
        tracklet_reports.append(tracklet_report)
        all_summaries[-1].update(tracklet_pose_frames=sum(r['tracklet_pose_valid'] for r in rows),
            tracklet_additional_pose_frames=sum(r['tracklet_pose_valid'] and not r['identity_valid'] for r in rows))
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
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(json_safe(row) for row in all_rows)
    for name,payload in [('per_frame_metrics.json',all_rows),('diagnostic-metrics.json',diagnostics),
                         ('quality.json',(dict(frame=r['frame'],worm_id=r['worm_id'],**describe(r)) for r in all_rows))]:
        write_json_records(output/name, payload)
    with (output/'bend_events.csv').open('w',newline='',encoding='utf-8-sig') as f:
        fields=['sample','worm_id','head_status','frame','time_s','direction','angle_deg','excursion_deg','cycle_amplitude_deg','cycle_period_s']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(all_events)
    report=dict(sample=sample,crop_xyxy=source['crop_xyxy'],mask_source=str(data),centerline_method=method,centerline_source=wormtracer_source if method!='skeleton' else 'skeleton',fps=fps,scale_um_per_px=None if source.get('measurement_mode')=='pixels' else scale,calibration=scale_info,
                measurement_mode=source.get('measurement_mode','physical'), metric_definition_version=METRIC_VERSION,
                overlay_version=OVERLAY_VERSION,
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
    if method=='body_sequence':
        from body_sequence_solver import signature,VERSION as BODY_VERSION
        report.update(body_sequence_version=BODY_VERSION,body_sequence_signature=signature(),body_center_definition='half_centerline_arclength',tracking_point_count=13)
    report=json_safe(report)
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    np.savez_compressed(output/'diagnostic-centerlines.npz',**{f'w{w}_f{f}':p for (w,f),p in lines.items()})
    lookup={(r['worm_id'],r['frame']):r for r in all_rows}
    lines={key:p for key,p in lines.items() if permissions(lookup[key])['posture']}
    np.savez_compressed(output/'centerlines.npz',**{f'w{w}_f{f}':p for (w,f),p in lines.items()})
    np.savez_compressed(output/'display-centerlines.npz',**{f'w{w}_f{f}':p for (w,f),p in display_lines.items()})
    if not defer_video:
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
    title = report['sample'] + ' | gaps = excluded; head metrics require manual confirmation'
    title_style = {}
    if not title.isascii():
        from matplotlib import font_manager
        from resource_paths import RESOURCE_ROOT
        font = RESOURCE_ROOT / 'fonts/NotoSansCJKsc-Regular.otf'
        if font.is_file():
            title_style['fontproperties'] = font_manager.FontProperties(fname=font, size=matplotlib.rcParams['figure.titlesize'])
        else:
            installed = {entry.name for entry in font_manager.fontManager.ttflist}
            families = ('Noto Sans CJK SC', 'Microsoft YaHei', 'PingFang SC', 'SimHei', 'WenQuanYi Zen Hei')
            title_style['fontfamily'] = ['DejaVu Sans', *(name for name in families if name in installed)]
    fig.suptitle(title, **title_style)
    fig.savefig(output/'phenotype-plots.png',dpi=160); plt.close(fig)
    simple=[{k:v for k,v in w.items() if not isinstance(v,(list,dict))} for w in report['worms']]
    with (output/'worm_summary.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(simple[0])); writer.writeheader(); writer.writerows(simple)
    with (output/'behavior_events.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        fields = ['worm_id','event','start_frame','end_frame','start_s','end_s','duration_s',
                  'left_censored','right_censored']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for worm in report['worms']:
            writer.writerows(dict(worm_id=worm['worm_id'], **event) for event in worm['behavior_events'])
    with (output/'trajectory_segments.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        fields = ['worm_id','start_frame','end_frame','start_s','end_s','net_displacement','unit']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for worm in report['worms']:
            writer.writerows(dict(worm_id=worm['worm_id'], unit=unit, **part) for part in worm['trajectory_segments'])
    scale_text='仅像素单位；未提供物理标定。' if pixel_mode(report) else f"像素比例：{report['scale_um_per_px']:.6f} μm/px。"
    if not pixel_mode(report) and report.get('calibration',{}).get('source'):
        scale_text+=f" 标定来源：{report['calibration']['source']}。"
    from result_metrics import present_report
    text = [f"# {report['sample']} 表型分析", '', scale_text]
    for worm in present_report(report, report['worms'])['worms']:
        text.extend(['', f"## 线虫 {worm['worm_id']:g}", '',
                     '|指标|数值|单位|有效帧|有效时长（s）|', '|---|---:|---|---:|---:|'])
        for metric in worm['metrics']:
            value = metric['value']
            frames = metric['valid_frames']
            seconds = metric['valid_duration_s']
            cells = [metric['label'], '—' if value is None else f'{value:.5g}',
                     metric['unit'], '—' if frames is None else f'{frames:g}',
                     '—' if seconds is None else f'{seconds:.3f}']
            text.append('|' + '|'.join(cells) + '|')
        for movement in worm['movement']:
            count=movement['count']
            text.append(f"\n{movement['label']}：{'—' if count is None else f'{count:g} 帧'}。{movement['note']}")
        text.append(f"\n活动分类（研究规则）：{worm['activity']['label']}。")
    text += ['', '头摆只统计头端已确认的连续片段。角度相对片段中性位超过阈值计一次，回正后同侧再次摆动可另计。频率分母为可检测的连续观察秒数。',
             '头颈弯曲角采用中线 0–10% 与 10–30% 两段方向的夹角。周期摆幅取连续同侧峰之间两段峰谷半差的平均，再按完整周期取中位数。另报弯曲角绝对值 P95。',
             '单虫身体序列在头颈局部近直且前后帧支持时，采用当帧轮廓截面中点拟合方向。持续的几何冲突进入区间复核，受影响的头角留空。',
             '体积采用圆形截面分段圆台估算，不是实测三维体积。速度位于图像坐标系，未校正载物台漂移。',
             '前后方向使用已确认头端和躯干中段的平移。转圈单独统计，采用基础与双倍时长两个连续轨迹窗口。A/B/C 与转圈为待验证研究规则。',
             '运动与方向时长在各自的连续有效窗口内按相邻帧时间中点分配。停顿次数只计可见起点；事件表中的 left_censored / right_censored 表示该端被窗口截断。倒退事件需同一头尾片段内从前进直接切换为后退。',
             '', '[停顿与倒退事件](behavior_events.csv) · [分段净位移](trajectory_segments.csv)',
             '', '[逐帧数据](per_frame_metrics.csv) · [单虫汇总](worm_summary.csv) · [摆动事件](bend_events.csv) · [质量](quality.json)',
             '', '![表型曲线](phenotype-plots.png)']
    (output/'报告.md').write_text('\n'.join(text),encoding='utf-8')


def render(sample,source,output,ids,rows,lines):
    _,_,_,get_mask,_=load_sample(sample,source.get('mask_source'),root=ROOT)
    fps=source['fps'];x0,y0,x1,y1=source['crop_xyxy']
    lookup={(r['worm_id'],r['frame']):r for r in rows}
    width,height=x1-x0,y1-y0
    with tempfile.TemporaryDirectory(dir=output) as scratch:
        temp=Path(scratch)/'overlay.mp4'
        cap=cv2.VideoCapture(source['video'])
        writer=cv2.VideoWriter(str(temp),cv2.VideoWriter_fourcc(*'mp4v'),fps,(width+width%2,height+height%2))
        try:
            if not writer.isOpened():raise RuntimeError('Cannot create phenotype video')
            for frame in range(source['frames']):
                report_progress(frame,source['frames'],'绘制虫体轮廓、中线与端点',fraction=.6+.39*frame/source['frames'])
                ok,image=cap.read()
                if not ok:raise RuntimeError('Truncated source video')
                raw=image[y0:y1,x0:x1]
                image=raw.copy()
                preview_masks = []
                for worm in ids:
                    mask = get_mask(worm,frame)
                    if preview_enabled():
                        preview_masks.append((worm, mask))
                    draw_worm(image,mask,lines.get((worm,frame)),lookup[(worm,frame)])
                report_preview(image,frame,source['frames'],'表型叠加画面 · 质量状态以结果页为准', raw=raw, masks=preview_masks)
                writer.write(cv2.copyMakeBorder(image,0,height%2,0,width%2,cv2.BORDER_CONSTANT))
                if frame in (0,source['frames']//2,source['frames']-1):write_png(output/f'preview-{frame:04d}.png',image)
        finally:
            writer.release()
            cap.release()
        check=cv2.VideoCapture(str(temp))
        count=int(check.get(cv2.CAP_PROP_FRAME_COUNT));check.release()
        if count!=source['frames']:raise RuntimeError('Export video frame count mismatch')
        temp.replace(output/'phenotypes-overlay.mp4')


def render_saved(sample, method, fit_source, destination):
    _,source,ids,_,_=load_sample(sample,fitted_mask_source(sample,fit_source) if method!='skeleton' else None,root=ROOT)
    folder=ROOT/'outputs'/sample/('phenotypes-'+fit_source if method!='skeleton' else 'phenotypes')
    with (folder/'per_frame_metrics.csv').open(encoding='utf-8-sig',newline='') as handle:
        rows=list(csv.DictReader(handle))
    for row in rows:
        row['worm_id'],row['frame']=int(row['worm_id']),int(row['frame'])
    display=folder/'display-centerlines.npz'
    with np.load(display if display.is_file() else folder/'centerlines.npz',allow_pickle=False) as saved:
        lines={tuple(int(v[1:]) for v in key.split('_')):saved[key] for key in saved.files}
    render(sample,source,destination,ids,rows,lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample',default='all',help='Sample directory name; all retains the two original samples')
    parser.add_argument('--config',type=Path,default=ROOT/'phenotype_config.json')
    parser.add_argument('--centerline-method',choices=['skeleton','wormtracer','niu','hybrid','topology','self_contact','body_sequence'],default='wormtracer')
    parser.add_argument('--wormtracer-source',default=None,help='Default: contact correction if present, otherwise temporal')
    parser.add_argument('--render-only',action='store_true',help='Refresh videos and previews using saved measurements')
    parser.add_argument('--defer-video',action='store_true')
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    for sample in (['2-1-1314','5-1-910'] if args.sample=='all' else [args.sample]):
        from centerline_methods import method_source
        default_source=method_source(args.centerline_method) if args.centerline_method in ('niu','hybrid','topology','self_contact','body_sequence') else None
        fit_source=args.wormtracer_source or default_source or ('wormtracer-contact-corrected' if (ROOT/'outputs'/sample/'wormtracer-contact-corrected'/'mask-source.json').exists() else 'wormtracer-temporal')
        if args.render_only:
            output=ROOT/'outputs'/sample/('phenotypes-'+fit_source if args.centerline_method!='skeleton' else 'phenotypes')
            render_saved(sample,args.centerline_method,fit_source,output)
            print(f'{sample}: refreshed annotated video and previews',flush=True)
        else:analyze(sample,config,args.centerline_method,fit_source,defer_video=args.defer_video)


if __name__=='__main__':main()
