"""Synthetic UI/contract fixture. Never include it in the real evaluation set."""
from pathlib import Path
import cv2
import numpy as np
from evaluation_dataset import VERSION, build_tasks, digest, freeze_split, sha256, write_new


def make_dataset(root):
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    path=root/'synthetic-ui-only.avi'
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),10,(640,360))
    if not writer.isOpened(): raise RuntimeError('Fixture encoder unavailable')
    for n in range(8):
        image=np.full((360,640,3),225,np.uint8)
        points=np.array([[160,160+n],[220,140],[280,170],[340,205],[400,185]],np.int32)
        cv2.polylines(image,[points],False,(65,75,65),18,cv2.LINE_AA)
        cv2.putText(image,'SYNTHETIC UI FIXTURE',(20,32),cv2.FONT_HERSHEY_SIMPLEX,.65,(100,100,100),1)
        writer.write(image)
    writer.release()
    source_hash=sha256(path)
    video=dict(video_id='vid-'+source_hash[:16],sha256=source_hash,aliases=[dict(path=str(path.resolve()))],primary_path=str(path.resolve()),
        metadata=dict(width=640,height=360,frame_count=8,fps_reported=10,readable=True),lineage=dict(parent_video_ids=[]),development_exposure='observed')
    manifest=dict(schema_version=VERSION,videos=[video],purpose='synthetic_software_test_only')
    splits=freeze_split(manifest)
    plan=dict(video_ids=[video['video_id']],random_frames_per_video=2,seed=20261019,events=[])
    tasks=build_tasks(manifest,splits,plan)
    for name,data in [('manifest',manifest),('splits',splits),('tasks',tasks)]: write_new(root/(name+'.json'),data)
    return manifest,splits,tasks


def marked_frame(n):
    return dict(frame=n,source_status='usable',complete=True,unknown_regions=[],notes='Synthetic fixture only',objects=[dict(
        local_id='1',mask=dict(status='visible',polygons=[[[150,150],[230,125],[410,175],[410,205],[320,220],[220,155]]],holes=[]),
        centerline=dict(status='visible',segments=[[[160,160],[220,140],[280,170],[340,205],[400,185]]],direction='unoriented'),
        endpoints=dict(a=dict(visibility='visible',point=[160,160]),b=dict(visibility='visible',point=[400,185])),
        identity=dict(status='unknown',individual_id=None,evidence=''),head_tail=dict(status='unknown',head_endpoint=None,evidence=''),
        self_crossing=dict(status='none',connections=[],evidence=''))])
