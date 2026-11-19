"""Exercise a freshly extracted desktop release on its native OS, without user settings."""
import csv
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

def run(folder):
    folder = folder.resolve()
    mac = sys.platform == 'darwin'
    expected = os.environ['TAG'].removeprefix('v')
    if mac:
        import plistlib
        assert platform.machine() == 'arm64'
        app = folder/'WormStudio.app/Contents'
        info = plistlib.loads((app/'Info.plist').read_bytes())
        assert info['CFBundleShortVersionString'] == expected and os.access(app/'MacOS'/info['CFBundleExecutable'],os.X_OK)
        res = app/'Resources'
        py = res/'python/bin/python3.11'
        electron = res/'electron/Electron.app/Contents/MacOS/Electron'
        code = res/'app'
    else:
        assert os.name == 'nt'
        app = folder/'WormStudio'
        res = app/'resources/analysis'
        py = res/'python/python.exe'
        electron = app/'WormStudio.exe'
        code = res/'code'
        assert json.loads((app/'resources/app/package.json').read_text())['version'] == expected
    work = folder/'smoke-workspace'
    shutil.copytree(code, work)
    assert json.loads((work/'tools/electron/package.json').read_text())['version'] == expected
    env = dict(os.environ, WORM_STUDIO_PYTHON=str(py), WORMSTUDIO_SAM2_CHECKPOINT=str(res/'models/sam2_hiera_large.pt'),
               WORM_STUDIO_RESOURCES=str(res), WORMSTUDIO_DEVICE='cpu', PYTHONNOUSERSITE='1', MPLBACKEND='Agg')
    subprocess.run([str(electron),'-p','process.versions.electron'],env={**env,'ELECTRON_RUN_AS_NODE':'1'},check=True,timeout=90)
    paths=[str(res/'sam2')]
    if mac:
        site=folder/'offline-packages'
        subprocess.run([str(py),'-I','-m','pip','--isolated','--disable-pip-version-check','install','--no-index','--no-cache-dir','--no-compile',
            '--only-binary=:all:','--require-hashes','--ignore-installed','--find-links',str(res/'wheels'),'--target',str(site),'-r',str(res/'requirements.lock')],check=True)
        paths.insert(0,str(site))
    env['PYTHONPATH']=os.pathsep.join(paths)
    video=work/'examples/practice.avi'
    cfg=dict(video=str(video),sample='practice-release',measurement_mode='pixels',sam2_model='large',
             centerline_method='body_sequence',head_detection='taper_seed',crop_xyxy=[0,0,256,256],
             groups=[dict(name='practice',crop_xyxy=[0,0,256,256],anchor_frame=0,
                          worms=[dict(id=1,points=[[100,142],[130,146],[100,80]],labels=[1,1,0],box=[35,98,220,165])])])
    (work/'smoke.json').write_text(json.dumps(cfg),encoding='utf8')
    subprocess.run([str(py),'-X','utf8','worm_pipeline.py','--config','smoke.json','--no-progress-ui','--no-resume'],cwd=work,env=env,check=True,timeout=900)
    dest=work/'outputs/practice-release/phenotypes-centerlines-body-sequence'
    assert json.loads((dest/'summary.json').read_text(encoding='utf8'))['overlay_version']=='contour-only-v1'
    report=json.loads((dest/'verification.json').read_text(encoding='utf8'))
    assert report['frames']==5 and report['available_centerlines']>0
    with (dest/'per_frame_metrics.csv').open(encoding='utf-8-sig') as f:
        assert [int(r['frame']) for r in csv.DictReader(f)]==list(range(5))
    expr="""import cv2,sys,numpy as np
raw,out=[cv2.VideoCapture(p) for p in sys.argv[1:]]
for i in range(5):
 a,x=raw.read(); b,y=out.read(); assert a and b and x.shape==y.shape
 assert np.abs(x[:35].astype(float)-y[:35]).mean()<8, 'top of image obscured'
 assert np.count_nonzero(y[:,:,1].astype(float)-y[:,:,2]>45)>50, 'missing contour'
assert not out.read()[0]
"""
    subprocess.run([str(py),'-c',expr,str(video),str(next(dest.glob('*_annotated_full-frame.mp4')))],env=env,check=True)
    print('Verified packaged desktop runtime, Large inference, five clean video frames and metrics.',flush=True)

if __name__=='__main__':
    run(Path(sys.argv[1]))
