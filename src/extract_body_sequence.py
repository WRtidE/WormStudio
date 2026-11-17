"""Production adapter for the single-worm SAM2/body-sequence method."""
import argparse
import json
import time

import cv2
import numpy as np

from analyze_phenotypes import ROOT, load_sample, json_safe
from body_sequence_solver import solve, VERSION, signature
from live_preview import report_geometry
from pipeline_progress import report_progress


def run(sample,worm):
    base=ROOT/'outputs'/sample
    config=json.loads((base/'pipeline-config.json').read_text(encoding='utf8'))
    data,source,ids,get_mask,_=load_sample(sample,apply_guards=False)
    if ids!=[worm]:raise ValueError('当前方法每次处理一个腔室的一条线虫')
    started=time.monotonic();gray=[];masks=[]
    cap=cv2.VideoCapture(source['video']);x,y,r,b=source['crop_xyxy']
    try:
        for i in range(source['frames']):
            ok,image=cap.read()
            if not ok:raise ValueError(f'源视频在 F{i} 读取失败')
            gray.append(cv2.cvtColor(image[y:b,x:r],cv2.COLOR_BGR2GRAY))
            masks.append(get_mask(worm,i))
            report_progress(i+1,source['frames'],'读取虫体与原始画面',fraction=.1*(i+1)/source['frames'])
    finally:cap.release()
    def progress(current,total,detail,fraction):
        report_progress(current,total,detail,fraction=.1+.85*fraction)
    from head_tail import anchors_in_crop
    manual=anchors_in_crop(config.get('head_annotations',{}).get(str(worm)),config['crop_xyxy'],source['crop_xyxy'])
    lines,facts,audit=solve(masks,gray,source['fps'],manual=manual,
        point_count=config.get('tracking_point_count',13),source_invalid=config.get('source_invalid_frames',()),
        auto_seed=config.get('head_detection','taper_seed') not in ('manual_only','manual_seed'),progress=progress)
    quality=[]
    for i,f in enumerate(facts):
        valid=f is not None
        quality.append(dict(frame=i,mask_valid=bool(masks[i].any()),centerline_valid=valid,
            quality_reasons=[] if valid else ['no_supported_body_path'],
            topology='simple' if f and f['candidate']=='clear_observation' else 'paired_contact' if valid else 'uncertain',
            selected_candidate=f['candidate'] if f else None,branch_score_margin=None,
            endpoint_a_inferred=bool(f and not f['head_observed']),endpoint_b_inferred=bool(f and not f['tail_observed']),
            uncertain_point_ranges=([[0,4]] if f and not f['head_observed'] else [])+
                                   ([[96,100]] if f and not f['tail_observed'] else []),
            temporal_anchors=f.get('anchor_frames',[]) if f else [],body_sequence=f))
        report_geometry(masks[i],lines[i] if valid else None,i,len(facts),'多帧身体中线与标记端',accepted=valid)
    folder=base/'centerlines-body-sequence';target=folder/f'worm-{worm}'
    target.mkdir(parents=True,exist_ok=True)
    np.save(target/'centerlines.npy',lines)
    (folder/'mask-source.json').write_text(json.dumps(dict(mask_source=str(data.resolve()))),encoding='utf8')
    for name,value in [('quality.json',quality),('sequence-audit.json',audit),('run.json',dict(
        version=VERSION,algorithm_signature=signature(),method='body_sequence',frames=len(lines),worm_id=worm,
        tracking_point_count=config.get('tracking_point_count',13),head_annotations=config.get('head_annotations',{}),
        valid_frames=sum(q['centerline_valid'] for q in quality),head_candidate_frames=sum(bool(f and f['head_known']) for f in facts),
        seconds=time.monotonic()-started,mask_source=str(data.resolve()),
        coordinates='Analysis crop pixels',center_definition='half_centerline_arclength',
        missing_policy='No supported path remains NaN; hidden endpoint estimates are explicitly marked'))]:
        (target/name).write_text(json.dumps(json_safe(value),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    report_progress(len(lines),len(lines),'已保存101点中线、标记端证据与未判帧',force=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample',required=True);parser.add_argument('--worm',type=int,required=True)
    args=parser.parse_args();run(args.sample,args.worm)
