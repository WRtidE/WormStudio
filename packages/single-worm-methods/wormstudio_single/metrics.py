"""Original v3 metric gating, temporal windows and per-frame definitions."""
from pathlib import Path
import csv
import numpy as np
from scipy import ndimage as ndi
from .common import runs
STATUS={'auto_supported':'自动质检通过','review':'姿态待复核','mask_review':'分割待复核','no_anchor':'缺少可靠初始化','unavailable':'中线不可用','temporal_recovered':'时序恢复候选'}

def choose_anchors(valid,max_gap=60):
    good=np.flatnonzero(valid)
    if not len(good):return []
    selected={int(good[0]),int(good[-1])}
    current=int(good[0])
    while current<good[-1]:
        available=good[(good>current)&(good<=current+max_gap)]
        current=int(available[-1] if len(available) else good[good>current][0]);selected.add(current)
    # Put observations immediately before/after sustained uncertain intervals.
    for group in runs(~np.asarray(valid,bool)):
        if len(group)<3:continue
        for k in [int(group[0])-1,int(group[-1])+1]:
            if 0<=k<len(valid) and valid[k]:selected.add(k)
    return sorted(selected)

def make_windows(n,anchors):
    if not anchors:return []
    windows=[]
    if anchors[0]>0:windows.append((np.arange(anchors[0],-1,-1),False))
    windows.extend((np.arange(a,b+1),True) for a,b in zip(anchors,anchors[1:]))
    if anchors[-1]<n-1:windows.append((np.arange(anchors[-1],n),False))
    if not windows:windows=[(np.array([anchors[0]]),False)]
    return windows

def crop_union(masks,pad=40):
    h,w=masks.shape[1:];yy,xx=np.nonzero(np.any(masks,axis=0))
    if not len(xx):return 0,0,w,h
    return max(0,int(xx.min())-pad),max(0,int(yy.min())-pad),min(w,int(xx.max())+pad+1),min(h,int(yy.max())+pad+1)

def mask_speeds(centers,valid,fps):
    speed=np.full(len(valid),np.nan)
    for ids in runs(np.asarray(valid)&np.isfinite(centers).all(1)):
        if len(ids)>=2:speed[ids]=np.linalg.norm(np.gradient(centers[ids],1/fps,axis=0),axis=1)
    return speed

def pose_supported(baseline_q,qa,disagreement,reference):
    return bool(baseline_q['centerline_valid'] and baseline_q['mask_valid'] and baseline_q['topology']=='simple'
        and qa.get('geometric_pass',False) and np.isfinite(disagreement) and disagreement<=max(3,.03*reference))

def write_csv(path,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader()
        for row in rows:writer.writerow({k:('' if isinstance(v,float) and not np.isfinite(v) else v) for k,v in row.items()})

def calculate_metrics(curves,masks,base_quality,qa,baseline,meta):
    n=len(curves);reference=meta['reference_length_px'];rows=[];centers=[]
    for i,(p,mask) in enumerate(zip(curves,masks)):
        yy,xx=np.nonzero(mask);centers.append([xx.mean(),yy.mean()] if len(xx) else [np.nan,np.nan])
        finite=np.isfinite(p).all();q=qa[i]
        # A/B are unconfirmed endpoint labels, so reversing the point order
        # cannot by itself turn an identical body shape into disagreement.
        disagreement=min(float(np.linalg.norm(p-baseline[i],axis=1).mean()),float(np.linalg.norm(p[::-1]-baseline[i],axis=1).mean())) if finite and np.isfinite(baseline[i]).all() else np.nan
        usable=pose_supported(base_quality[i],q,disagreement,reference or 1.)
        temporal_recovered=q.get('status')=='TEMPORAL_RECONSTRUCTION_REVIEW'
        if temporal_recovered:usable=False
        if not meta['anchors']:status='no_anchor'
        elif not base_quality[i]['mask_valid']:status='mask_review'
        elif not finite:status='unavailable'
        elif temporal_recovered:status='temporal_recovered'
        else:status='auto_supported' if usable else 'review'
        reasons=[]
        if not meta['anchors']:reasons.append('no_reliable_initial_pose')
        if not base_quality[i]['mask_valid']:reasons.append('mask_quality')
        if base_quality[i]['topology']!='simple':reasons.append('self_contact_or_uncertain_topology')
        if not finite:reasons.append('centerline_unavailable')
        elif not q.get('geometric_pass'):reasons.append('centerline_geometry')
        if temporal_recovered:reasons.append('whole_event_temporal_inference')
        if not base_quality[i]['centerline_valid']:reasons.append('baseline_pose_uncertain')
        if np.isfinite(disagreement) and disagreement>max(3,.03*(reference or 1)):reasons.append('method_disagreement')
        length=width=curvature=tip_distance=np.nan
        if finite:
            ds=np.linalg.norm(np.diff(p,axis=0),axis=1);length=float(ds.sum());tip_distance=float(np.linalg.norm(p[0]-p[-1]))
            theta=np.unwrap(np.arctan2(np.diff(p[:,1]),np.diff(p[:,0])))
            curvature=float(np.sum(abs(np.diff(theta)))/max(length,1))
            if usable:
                dt=ndi.distance_transform_edt(mask)
                width=float(2*ndi.map_coordinates(dt,[p[:,1],p[:,0]],order=1,mode='constant')[5:-5].mean())
        row=dict(frame=i,time_s=i/meta['fps'],status=status,status_zh=STATUS[status],pose_metrics_valid=usable,
            mask_metrics_valid=bool(base_quality[i]['mask_valid']),head_identity='unconfirmed_A_B',
            topology=base_quality[i]['topology'],review_reason=';'.join(reasons),
            length_px=length if usable else np.nan,mean_width_px=width,mean_abs_curvature_rad_px=curvature if usable else np.nan,
            end_to_end_px=tip_distance if usable else np.nan,candidate_length_px=length,candidate_curvature_rad_px=curvature,
            observed_area_px2=int(mask.sum()),area_px2=int(mask.sum()) if base_quality[i]['mask_valid'] else np.nan,
            center_x_px=centers[-1][0]+meta['tracking_roi'][0]+meta['crop_xyxy'][0],
            center_y_px=centers[-1][1]+meta['tracking_roi'][1]+meta['crop_xyxy'][1],
            inside_fraction=q.get('inside_fraction',np.nan),geometry_pass=q.get('geometric_pass',False),
            candidate=q.get('candidate','none'),baseline_mean_difference_px=disagreement)
        observed=q.get('endpoint_observed',[True,True] if finite else [False,False])
        row.update(endpoint_a_observed=bool(observed[0]),endpoint_b_observed=bool(observed[1]),
            temporal_reconstructed=temporal_recovered)
        for key in ('tap_effective_points','tap_bidirectional_points','tap_bidirectional_error_px',
                    'tap_distribution_effective_points','tap_distribution_overlap',
                    'tap_conflict_fraction','tap_backward_reversed','tap_candidate_cost',
                    'alternative_energy_gap','alternative_candidate'):
            if key in q:row[key]=q[key]
        scale=meta['scale_um_per_px']
        row['length_um_estimate']=row['length_px']*scale if scale else np.nan
        rows.append(row)
    speed=mask_speeds(np.array(centers),[r['mask_metrics_valid'] for r in rows],meta['fps'])
    for row,v in zip(rows,speed):row.update(centroid_speed_px_s=float(v),centroid_speed_um_s_estimate=float(v*meta['scale_um_per_px']) if meta['scale_um_per_px'] else np.nan)
    return rows
