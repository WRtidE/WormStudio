"""Fit official WormTracer 19.5 to individual cached SAM2 tracks on CUDA."""
import argparse
from resource_paths import RESOURCE_ROOT
from pipeline_progress import report_progress
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import torch
import yaml
from analyze_phenotypes import ROOT, load_sample, write_png, resample
from pose_segments import accepted_segments, rejected_quality

sys.path.insert(0, str(RESOURCE_ROOT/'WormTracer'/'WormTracer19_5'))
from WormTracer import wt

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', default='all')
    parser.add_argument('--worm', type=int)
    parser.add_argument('--rescale', type=float, default=.3)
    parser.add_argument('--output-name', default='wormtracer')
    parser.add_argument('--continuity-weight', type=float, default=10000)
    parser.add_argument('--smoothness-weight', type=float, default=100000)
    parser.add_argument('--length-weight', type=float, default=50)
    parser.add_argument('--crop-to-track', action='store_true',help='Fit each object within its full-track bounding box, then restore coordinates')
    parser.add_argument('--overwrite', action='store_true',help='Refit and replace adapted outputs when an upstream stage was rebuilt')
    args=parser.parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is required for this run')
    params=yaml.safe_load((RESOURCE_ROOT/'WormTracer/WormTracer19_1/WormTracer/config.yaml').read_text())
    params.update(local_time_difference=8, rescale=args.rescale,
                  continuity_loss_weight=args.continuity_weight,
                  smoothness_loss_weight=args.smoothness_weight, length_loss_weight=args.length_weight,
                  SaveCenterlinedWormsSerial=False, SaveCenterlinedWormsMovie=False,
                  SaveCenterlinedWormsMultitiff=False)
    # Disable notebook-only display, leaving optimization and file outputs intact.
    wt.show_image=lambda *a, **kw: None
    plt.show=lambda *a, **kw: plt.close('all')
    for sample in (['5-1-910','2-1-1314'] if args.sample=='all' else [args.sample]):
        mask_source, source, ids, get_mask, bad=load_sample(sample)
        output=ROOT/'outputs'/sample/args.output_name;output.mkdir(exist_ok=True)
        for worm in ids:
            if args.worm and worm!=args.worm: continue
            target=output/f'worm-{worm}'
            if (target/'centerlines.npy').exists() and not args.overwrite:
                print(f'Skipping completed {sample}/{worm}',flush=True);continue
            started=time.time();torch.cuda.reset_peak_memory_stats()
            segments=accepted_segments(source['frames'],worm,bad)
            points=np.full((source['frames'],101,2),np.nan)
            quality=rejected_quality(source['frames'])
            fit_crop=None
            if args.crop_to_track:
                extent=[]
                for frame in [int(f) for segment in segments for f in segment]:
                    mask=get_mask(worm,frame);yy,xx=np.nonzero(mask)
                    if len(xx):extent.append([xx.min(),yy.min(),xx.max()+1,yy.max()+1])
                if extent:
                    extent=np.array(extent);x0,y0=np.maximum(extent[:,:2].min(0)-15,0)
                    x1,y1=np.minimum(extent[:,2:].max(0)+15,[mask.shape[1],mask.shape[0]])
                    fit_crop=[int(x0),int(y0),int(x1),int(y1)]
            target.mkdir(parents=True,exist_ok=True)
            for segment in segments:
              with tempfile.TemporaryDirectory(prefix='wormtracer_') as temporary:
                temp=Path(temporary);inputs=temp/'masks';inputs.mkdir()
                for local,frame in enumerate(segment):
                    frame=int(frame)
                    report_progress(frame,source['frames'],'准备 WormTracer 输入掩膜',fraction=.04*frame/source['frames'])
                    mask=get_mask(worm,frame)
                    if fit_crop is not None:mask=mask[y0:y1,x0:x1]
                    write_png(inputs/f'{local:06d}.png',mask.astype(np.uint8)*255)
                config=temp/'config.yaml';config.write_text(yaml.safe_dump(params))
                print(f'Fitting {sample} worm {worm} on {torch.cuda.get_device_name()}',flush=True)
                report_progress(detail=f'线虫 {worm} · CUDA 分块姿态优化，进度详见下方日志',force=True)
                wt.run(str(config),str(inputs),str(temp/'results'))
                report_progress(source['frames'],source['frames'],'姿态优化完成，正在保存中线',fraction=.98,force=True)
                result=next((temp/'results').iterdir())
                x=np.loadtxt(next(result.glob('*_x.csv')),delimiter=',')
                y=np.loadtxt(next(result.glob('*_y.csv')),delimiter=',')
                fitted=np.stack((np.atleast_2d(x),np.atleast_2d(y)),axis=-1)
                if fitted.shape!=(len(segment),params['plot_n'],2) or not np.isfinite(fitted).all():
                    raise RuntimeError(f'Invalid WormTracer output {fitted.shape}')
                shutil.copytree(result,target/f'segment-{segment[0]}-{segment[-1]}',dirs_exist_ok=True)
                if fit_crop is not None:fitted=fitted+[x0,y0]
                points[segment]=np.array([resample(p) for p in fitted])
                for f in segment:
                    quality[f].update(mask_valid=True,centerline_valid=True,topology='unassessed',
                        quality_reasons=[],selected_candidate='wormtracer_raw')
            np.save(target/'centerlines.npy',points)
            (target/'quality.json').write_text(json.dumps(quality,indent=2),encoding='utf-8')
            metadata=dict(sample=sample,worm_id=worm,frames=source['frames'],params=params,
                              accepted_segments=[[int(s[0]),int(s[-1])] for s in segments],
                              input_mask_source=str(mask_source),video=source['video'],crop_xyxy=source['crop_xyxy'],input_fit_crop_xyxy=fit_crop,
                              device=torch.cuda.get_device_name(),seconds=time.time()-started,
                              peak_cuda_allocated_mb=torch.cuda.max_memory_allocated()/2**20,
                              upstream_commit='8fc27f5a083e7543ab3f2708a7b0867873673e2f',
                              coordinates='SAM2 crop pixels; first point is frequency-based head candidate')
            (target/'run.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
            print(f'Completed {sample}/{worm}: {metadata["seconds"]:.1f}s',flush=True)
            torch.cuda.empty_cache()

if __name__=='__main__':main()
