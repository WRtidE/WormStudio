"""Export raw SAM2 mask fits and the identity decisions used to accept them."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

import numpy as np

from crop_recovery import tracking_directory
from centerline_methods import SELF_CONTACT_VERSION, method_source
from pipeline_progress import report_progress
from resource_paths import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT / 'tools'))
from refit_self_contacts import fit_sample


def run(sample, worm):
    folder = PROJECT_ROOT / 'outputs' / sample
    out = folder / method_source('self_contact') / f'worm-{worm}'
    out.mkdir(parents=True, exist_ok=True)
    report_progress(detail='从原始画面和掩膜拟合全身中线，复核交叠区间', force=True)
    pose, events, guards = fit_sample(folder, worm=worm, allow_unresolved=True)
    if guards is None:
        raise ValueError('缺少掩膜质量检查，请先运行 quality 阶段')
    lines = pose['xy']
    status = pose['status']
    ok = np.isin(status, ('input', 'input_reordered', 'inferred')) & np.isfinite(lines).all(axis=(1, 2))
    candidates = np.full_like(lines, np.nan)
    candidates[~ok] = lines[~ok]
    review_links = {frame: (event["review_anchors"], event["review_right_reversed"])
                    for event in events["events"] if "review_anchors" in event
                    for frame in range(event["first"], event["last"] + 1)}
    quality = []
    for i, (state, valid, visible, guard) in enumerate(zip(status, ok, pose['endpoint_visible'], guards)):
        inferred = np.flatnonzero(~visible) * 100
        quality.append(dict(frame=i, mask_valid=bool(guard['mask_valid']), centerline_valid=bool(valid),
                            quality_reasons=[] if valid else [str(state)], selected_candidate=str(state),
                            topology='refitted' if state=='inferred' else 'simple' if valid else 'unresolved',
                            branch_score_margin=None, endpoint_a_inferred=not bool(visible[0]),
                            endpoint_b_inferred=not bool(visible[1]),
                            uncertain_point_ranges=[[int(k),int(k)] for k in inferred]))
        if i in review_links:
            quality[-1]["review_anchors"], quality[-1]["review_right_reversed"] = review_links[i]
    meta = dict(method='self_contact', version=SELF_CONTACT_VERSION, frames=len(lines), worm_id=worm,
                valid_frames=int(ok.sum()), mask_source=str(tracking_directory(folder, json.loads((folder/'pipeline-config.json').read_text(encoding='utf-8-sig')) if (folder/'pipeline-config.json').is_file() else {})), scale=events['scale'])
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        stage = Path(tmp)
        np.save(stage/'centerlines.npy', np.where(ok[:,None,None],lines,np.nan))
        np.save(stage/'diagnostic-candidates.npy', candidates)
        for name, data in [('events',events),('identity-guards',guards),('quality',quality),('run',meta)]:
            (stage/f'{name}.json').write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        for item in stage.iterdir():
            item.replace(out/item.name)
    report_progress(len(lines),len(lines),'中线与交叠复核已完成',force=True)
    print(f'线虫 {worm}：{ok.sum()}/{len(ok)} 帧有可用中线', flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', required=True)
    parser.add_argument('--worm', type=int, required=True)
    args=parser.parse_args()
    run(args.sample,args.worm)
