"""NIU ordered skeletons and quality-gated WormTracer/NIU combination.

The hybrid method selects a complete path; it never averages incompatible
paths through a crossing. Missing NIU paths remain NaN with explicit quality
records and are excluded from measurements. This is a project adaptation.
"""
import argparse
from pipeline_progress import report_progress
from live_preview import report_geometry
import json
import time
from pathlib import Path

import numpy as np

from analyze_phenotypes import ROOT, load_sample, json_safe
from centerline_methods import method_source
from compare_niu_skeleton import extract_niu, line_metrics, valid_line, align, sequence_metrics


def select_path(niu, metric, wt=None, wt_quality=None, previous=None):
    """Prefer observed simple NIU paths; retain WT uncertainty on complex frames."""
    simple = niu['topology'] == 'simple' and metric['geometric_pass']
    p = niu['ordered']
    if wt is None:
        return p, 'niu', [] if simple else ['niu_ambiguous_topology_or_geometry']
    # Prevent an algorithm switch from making a large instantaneous posture jump.
    if simple and valid_line(previous):
        q = align(p, previous)
        length = np.linalg.norm(np.diff(previous,axis=0),axis=1).sum()
        error = np.mean(np.linalg.norm((q-q.mean(0))-(previous-previous.mean(0)),axis=1))
        simple = error <= .10*max(length,1.)
    if simple:
        return p, 'niu', []
    if valid_line(wt):
        reasons = list((wt_quality or {}).get('quality_reasons', []))
        if not (wt_quality or {}).get('centerline_valid', False) and not reasons:
            reasons.append('wormtracer_requires_review')
        return wt.copy(), 'wormtracer', reasons
    return p, 'niu_fallback', ['no_reliable_hybrid_candidate']


def run(sample, method, worm_filter=None):
    if method not in ('niu','hybrid'): raise ValueError('This adapter supports niu/hybrid')
    data, source, ids, get_mask, bad = load_sample(sample)
    folder = ROOT/'outputs'/sample/method_source(method)
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'mask-source.json').write_text(json.dumps(dict(mask_source=str(data.resolve()))),encoding='utf-8')
    for worm in ids:
        if worm_filter is not None and worm != worm_filter: continue
        started=time.monotonic(); count=source['frames']
        wt = wt_quality = None
        if method == 'hybrid':
            fit=ROOT/'outputs'/sample/'wormtracer-temporal'/f'worm-{worm}'
            wt=np.load(fit/'centerlines.npy',allow_pickle=False)
            wt_quality=json.loads((fit/'quality.json').read_text(encoding='utf-8'))
            if wt.shape!=(count,101,2) or len(wt_quality)!=count:
                raise ValueError('WormTracer input frame count mismatch')
        points=np.full((count,101,2),np.nan,np.float32)
        quality=[]; previous=None; previous_center=None
        for frame in range(count):
            report_progress(frame,count,f'线虫 {worm} · '+('NIU 骨架提取' if method=='niu' else 'NIU 与 WormTracer 路径选择'))
            mask=get_mask(worm,frame)
            if bad.get((frame,worm),False):
                quality.append(dict(frame=frame,mask_valid=False,centerline_valid=False,
                    quality_reasons=['tracking_contact_or_area_outlier'],topology='mask_excluded',
                    selected_candidate='none',branch_score_margin=None))
                previous=previous_center=None
                report_geometry(mask,None,frame,count,f'线虫 {worm} · 轮廓待复核，未生成可靠中线')
                continue
            niu=extract_niu(mask)
            metric=line_metrics(niu['ordered'],mask,niu['raw_xy'],niu['radius'])
            p,selected,reasons=select_path(niu,metric,wt[frame] if wt is not None else None,
                                           wt_quality[frame] if wt_quality else None,previous)
            mask_valid=not bad.get((frame,worm),False)
            if not mask_valid: reasons.append('tracking_contact_or_area_outlier')
            if valid_line(p):
                p=align(p,previous)
                chosen_metric=line_metrics(p,mask,niu['raw_xy'],niu['radius'])
                if not chosen_metric['geometric_pass']: reasons.append('centerline_geometry_review')
                motion=sequence_metrics(p,previous,np.asarray(niu['centroid']),previous_center,None)
                if any(motion[k] > t for k,t in [('length_change_fraction',.2),('endpoint_change_fraction',.2),('shape_change_fraction',.15)]):
                    reasons.append('centerline_temporal_jump')
                points[frame]=p
                # Do not bridge missing/excluded frames when checking continuity.
                previous=p if mask_valid else None
                previous_center=np.asarray(niu['centroid']) if mask_valid else None
            else:
                reasons.append('centerline_missing')
                previous=previous_center=None
            topology=niu['topology']
            extra={}
            if selected=='wormtracer':
                item=wt_quality[frame]
                topology=item['topology']
                for key in ('uncertain_point_ranges','endpoint_a_inferred','endpoint_b_inferred','temporal_anchors'):
                    if key in item: extra[key]=item[key]
            quality.append(dict(frame=frame,mask_valid=mask_valid,centerline_valid=not reasons,
                quality_reasons=list(dict.fromkeys(reasons)),topology=topology,
                selected_candidate=selected,branch_score_margin=None,niu_topology=niu['topology'],
                niu_path_fraction=niu['path_fraction'],**extra))
            report_geometry(mask,points[frame],frame,count,f'线虫 {worm} · '+('中线通过当前检查' if not reasons else '候选中线待复核'),accepted=not reasons)
        report_progress(count,count,'中线提取完成，保存路径和质量记录',force=True)
        target=folder/f'worm-{worm}';target.mkdir(exist_ok=True)
        np.save(target/'centerlines.npy',points)
        (target/'quality.json').write_text(json.dumps(json_safe(quality),indent=2,allow_nan=False),encoding='utf-8')
        meta=dict(method=method,frames=count,worm_id=worm,mask_source=str(data.resolve()),
                  coordinates='analysis crop pixels; A/B endpoints, biological identity unconfirmed',
                  missing_policy='NaN path with explicit invalid quality; no invented centerline',
                  hybrid_policy='Simple supported NIU paths, otherwise temporal WormTracer; no coordinate averaging',
                  valid_frames=sum(q['centerline_valid'] for q in quality),seconds=round(time.monotonic()-started,3))
        (target/'run.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
        print(f'{method}: worm {worm}, {meta["valid_frames"]}/{count} valid frames',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample',required=True)
    parser.add_argument('--method',choices=['niu','hybrid'],required=True)
    parser.add_argument('--worm',type=int)
    args=parser.parse_args()
    run(args.sample,args.method,args.worm)
