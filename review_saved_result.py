"""Open an existing analysis without changing its measurements or rerunning models."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from resource_paths import ensure_runtime, PROJECT_ROOT
if __name__=='__main__':ensure_runtime()
import argparse
import json
import webbrowser
import cv2
from pipeline_progress import PipelineProgress
from manual_worm_annotation import create_server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample',required=True)
    parser.add_argument('--method',choices=['niu','wormtracer','hybrid','topology','tapnext'],default=None)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    base=(PROJECT_ROOT/'outputs'/args.sample).resolve()
    if not base.is_relative_to((PROJECT_ROOT/'outputs').resolve()):raise ValueError('Invalid sample')
    config=json.loads((base/'pipeline-config.json').read_text(encoding='utf-8'))
    args.method=args.method or config['centerline_method']
    config['centerline_method']=args.method
    cap=cv2.VideoCapture(config['video'])
    info=dict(frames=int(cap.get(7)),fps=cap.get(5),ids=[w['id'] for g in config['groups'] for w in g['worms']]);cap.release()
    progress=PipelineProgress()
    stages=[dict(name='metrics',phase='metrics'),dict(name='export',phase='export')]
    # The review server keeps no path for persist(), so saved runs remain unchanged.
    from centerline_methods import METHODS
    progress.config=config
    progress.data.update(sample=config['sample'],video_name=Path(config['video']).name,
        frames=info['frames'],fps=info['fps'],worm_count=len(info['ids']),
        method=METHODS[args.method]['label'],output_dir=str(base),read_only=True)
    progress.data['stages']=[dict(name=s['name'],label=s['name'],phase=s['phase'],status='complete',fraction=1,seconds=0) for s in stages]
    progress.finish(base,config,'export')
    progress.bind_server(create_server(progress=progress))
    print(progress.url,flush=True)
    if not args.no_browser:webbrowser.open(progress.url)
    try:progress.wait()
    finally:progress.close()


if __name__=='__main__':main()
