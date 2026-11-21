"""Run production multiworm guards before any single-object pose solver."""
import argparse
import json
from pathlib import Path

from quality.multiworm_quality import sequence_guards, VERSION as GUARD_VERSION
from quality.quality_schema import VERSION
from app.pipeline_progress import report_progress


def load_guards(data, ids, frames, required=False):
    path = Path(data)/'quality-guards.json'
    if not path.is_file():
        if required: raise ValueError('缺少统一质量检查，请先运行 quality 阶段')
        return None
    report = json.loads(path.read_text(encoding='utf-8'))
    if (report.get('quality_schema_version') != VERSION or report.get('guard_version') not in (GUARD_VERSION,'quality-guards-v6-single-body')
            or report.get('frames') != frames or sorted(report.get('worm_ids', [])) != sorted(ids)):
        raise ValueError('质量检查版本或输入帧数不匹配，请重新运行 quality 阶段')
    result = {w: report['worms'][str(w)] for w in ids}
    if any(len(rows) != frames or any(r['frame'] != f or r['worm_id'] != w for f,r in enumerate(rows))
           for w,rows in result.items()):
        raise ValueError('质量记录缺帧或虫编号不匹配')
    return result


def run(sample, mask_source=None, boundary_frames=None):
    from measurements.analyze_phenotypes import ROOT, load_sample, json_safe
    base = ROOT/'outputs'/sample
    config = json.loads((base/'pipeline-config.json').read_text(encoding='utf-8'))
    data, source, ids, get_mask, bad = load_sample(sample, mask_source or base/'sam2-tracking', apply_guards=False)
    # A lazy sequence avoids holding frames × worms × full-resolution masks in RAM.
    class Masks:
        def __init__(self, worm): self.worm = worm
        def __len__(self): return source['frames']
        def __getitem__(self, frame):
            if not 0 <= frame < len(self): raise IndexError(frame)
            report_progress(frame, len(self), f'检查虫间接触和身份连续性 · W{self.worm}')
            return get_mask(self.worm, frame)
    guards = sequence_guards({w: Masks(w) for w in ids}, bad,
        source_invalid=config.get('source_invalid_frames', []), fps=source['fps'],
        identity_confirmations=config.get('identity_confirmations', []), boundary_frames=boundary_frames,check_centroid_jump=config.get('centerline_method')!='body_sequence')
    report = dict(quality_schema_version=VERSION, guard_version='quality-guards-v6-single-body' if config.get('centerline_method')=='body_sequence' else GUARD_VERSION,
        frames=source['frames'], fps=source['fps'], worm_ids=ids, worms=guards,
        source_invalid_frames=config.get('source_invalid_frames', []),
        identity_confirmations=config.get('identity_confirmations', []),
        provenance='pipeline-config.json + SAM2 masks + tracking-quality.csv; engineering checks, not accuracy')
    target = data/'quality-guards.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(target)
    report_progress(source['frames'], source['frames'], '身份质量检查完成', force=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', required=True)
    run(parser.parse_args().sample)
