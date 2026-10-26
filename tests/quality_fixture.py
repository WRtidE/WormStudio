"""Deterministic synthetic contact fixture; this is not biological accuracy data."""
import csv
import json
from pathlib import Path
import cv2
import numpy as np
from quality_schema import VERSION


def make_fixture(root):
    root=Path(root);sample='synthetic-contact';base=root/'outputs'/sample
    tracking=base/'sam2-tracking';tracking.mkdir(parents=True,exist_ok=True)
    video=root/'contact.mp4';n=10;fps=10.
    writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'mp4v'),fps,(640,360))
    if not writer.isOpened(): raise RuntimeError('Fixture video encoder unavailable')
    masks={1:[],2:[]}
    for frame in range(n):
        a=np.zeros((360,640),np.uint8);b=a.copy()
        cv2.line(a,(160,185),(390,185),255,15)
        if frame in (2,3,8):cv2.line(b,(280,140),(280,320),255,15)
        else:cv2.line(b,(160,285),(390,285),255,15)
        image=np.full((360,640,3),235,np.uint8)
        image[(a|b)>0]=(70,75,80);writer.write(image)
        for worm,mask in ((1,a),(2,b)):
            folder=tracking/'masks'/f'worm-{worm}';folder.mkdir(parents=True,exist_ok=True)
            (folder/f'{frame:06d}.png').write_bytes(cv2.imencode('.png',mask)[1].tobytes())
            masks[worm].append(mask>0)
    writer.release()
    confirmation=dict(worm_id=1,frame=6,reviewer='synthetic-fixture-reviewer',
        confirmed_at='2026-10-19T12:00:00+08:00',evidence='Synthetic construction: horizontal object 1; not a real annotation')
    config=dict(sample=sample,video=str(video),crop_xyxy=[0,0,640,360],scale_um_per_px=1.25,
        scale_source='synthetic fixture scale',quality_schema_version=VERSION,centerline_method='niu',
        sam2_model='large',head_detection='manual_only',source_invalid_frames=[8],identity_confirmations=[confirmation],
        groups=[dict(name='synthetic',anchor_frame=0,crop_xyxy=[0,0,640,360],
            worms=[dict(id=w,mask_path=str(tracking/'masks'/f'worm-{w}'/'000000.png')) for w in (1,2)])])
    source=dict(video=str(video),frames=n,fps=fps,crop_xyxy=config['crop_xyxy'],worm_ids=[1,2],
        scale_um_per_px=1.25,scale_source='synthetic fixture scale',sam2_model='large')
    (base/'pipeline-config.json').write_text(json.dumps(config),encoding='utf-8')
    (tracking/'summary.json').write_text(json.dumps(source),encoding='utf-8')
    with (tracking/'tracking-quality.csv').open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=['frame','worm_id','low_confidence']);writer.writeheader()
        writer.writerows(dict(frame=f,worm_id=w,low_confidence=False) for f in range(n) for w in (1,2))
    return config,source,masks
