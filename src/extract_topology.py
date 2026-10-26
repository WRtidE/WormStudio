"""Export accepted single-worm centerlines; keep hypotheses in a separate file."""
import argparse,json,time
import numpy as np
from analyze_phenotypes import ROOT,load_sample,json_safe
from crossing_centerline import resolve_sequence,VERSION,DEFAULTS
from pipeline_progress import report_progress


def run(sample,worm_filter=None):
    data,source,ids,get_mask,bad=load_sample(sample)
    folder=ROOT/'outputs'/sample/'centerlines-topology';folder.mkdir(exist_ok=True)
    (folder/'mask-source.json').write_text(json.dumps(dict(mask_source=str(data.resolve()))),encoding='utf-8')
    for worm in ids:
        if worm_filter is not None and worm!=worm_filter:continue
        started=time.monotonic();masks=[]
        for frame in range(source['frames']):
            report_progress(frame,source['frames'],'读取单虫轮廓与自交候选',fraction=.15*frame/source['frames'])
            masks.append(get_mask(worm,frame))
        valid=[not bad.get((f,worm),False) for f in range(source['frames'])]
        report_progress(detail='比较交叉遍历顺序、前后姿态与候选差距',fraction=.2,force=True)
        points,quality,diagnostic=resolve_sequence(masks,valid,source['fps'])
        target=folder/f'worm-{worm}';target.mkdir(exist_ok=True)
        np.save(target/'centerlines.npy',points)
        np.save(target/'diagnostic-candidates.npy',diagnostic)
        (target/'quality.json').write_text(json.dumps(json_safe(quality),indent=2,allow_nan=False),encoding='utf-8')
        meta=dict(version=VERSION,frames=source['frames'],worm_id=worm,method='topology',mask_source=str(data.resolve()),
            seconds=time.monotonic()-started,valid_frames=sum(q['centerline_valid'] for q in quality),
            contact_frames=sum(q['topology']=='paired_contact' for q in quality),
            accepted_contact_frames=sum(q['topology']=='paired_contact' and q['centerline_valid'] for q in quality),
            thresholds=DEFAULTS,threshold_status='Engineering gates; real-video annotation validation pending',
            coordinates='Analysis crop pixels; A/B is not anatomical head/tail',
            missing_policy='Rejected/ambiguous paths are NaN; diagnostic candidates never enter metrics or rendering')
        (target/'run.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
        report_progress(source['frames'],source['frames'],'已保存可靠路径；不确定自交帧保留缺失',force=True)
        print(f'{sample}: {meta["valid_frames"]}/{source["frames"]} accepted; contacts {meta["accepted_contact_frames"]}/{meta["contact_frames"]}',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--sample',required=True);parser.add_argument('--worm',type=int)
    args=parser.parse_args();run(args.sample,args.worm)
