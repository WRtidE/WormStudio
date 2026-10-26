"""Remove remote whole-object duplicates while retaining smaller body fragments."""
import argparse
from pipeline_progress import report_progress
import csv
import json
import shutil
import numpy as np
from scipy import ndimage as ndi
from tracking_quality import reference_area, mark_quality
from analyze_phenotypes import ROOT,load_sample,write_png


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--sample',required=True);args=parser.parse_args()
    data,summary,ids,get_mask,_=load_sample(args.sample,apply_guards=False)
    with (data/'frame_metrics.csv').open() as handle:old=list(csv.DictReader(handle))
    reference={w:reference_area([int(r['area_px']) for r in old if int(r['worm_id'])==w]) for w in ids}
    rows=[];changes=[]
    for f in range(summary['frames']):
        report_progress(f,summary['frames'],'检查掩膜连通区域')
        masks={w:get_mask(w,f) for w in ids};clean={w:m.copy() for w,m in masks.items()}
        for w,mask in masks.items():
            yy,xx=np.nonzero(mask)
            if not len(xx):continue
            y,x=yy.min(),xx.min();y1,x1=yy.max()+1,xx.max()+1
            labels,n=ndi.label(mask[y:y1,x:x1],np.ones((3,3)));sizes=np.bincount(labels.ravel());sizes[0]=0
            substantial=np.flatnonzero(sizes>.5*reference[w])
            if len(substantial)<2:continue
            for other,other_mask in masks.items():
                if other==w:continue
                overlap=np.bincount(labels.ravel(),weights=other_mask[y:y1,x:x1].ravel(),minlength=len(sizes))
                cover=overlap/np.maximum(sizes,1)
                legitimate=[k for k in substantial if cover[k]<.1]
                if not legitimate:continue
                for k in substantial:
                    if cover[k]<.8 or sizes[k]<.6*reference[w]:continue
                    center=np.argwhere(labels==k).mean(0)
                    gap=min(np.linalg.norm(center-np.argwhere(labels==v).mean(0)) for v in legitimate)
                    if gap<2*np.sqrt(reference[w]/np.pi):continue
                    local=clean[w][y:y1,x:x1];local[labels==k]=False
            if not np.array_equal(clean[w],mask):
                path=data/'masks'/f'worm-{w}'/f'{f:06d}.png';backup=data/'raw-duplicate-components'/f'worm-{w}'
                backup.mkdir(parents=True,exist_ok=True)
                if not (backup/path.name).exists():shutil.copy2(path,backup/path.name)
                write_png(path,clean[w].astype(np.uint8)*255)
                changes.append(dict(frame=f,worm_id=w,removed_px=int((mask&~clean[w]).sum())))
        for w,mask in clean.items():
            yy,xx=np.nonzero(mask);other=np.any([m for v,m in clean.items() if v!=w],axis=0)
            rows.append(dict(frame=f,worm_id=w,area_px=int(mask.sum()),
                centroid_x_px=float(xx.mean()) if len(xx) else None,centroid_y_px=float(yy.mean()) if len(yy) else None,
                ambiguous_overlap_px=int((mask&other).sum())))
    mark_quality(rows,ids,summary.get('review_intervals',[]))
    for name in ('frame_metrics.csv','tracking-quality.csv'):
        with (data/name).open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (data/'component-cleanup.json').write_text(json.dumps(dict(changes=changes,
        rule='Only remove a remote whole-body-size component already covered >80% by another object, with a separate unshared component for this identity'),indent=2))
    print('Removed remote whole-object duplicates:',len(changes),flush=True)


if __name__=='__main__':main()
