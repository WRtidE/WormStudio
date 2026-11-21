"""Run production multiworm guards before any single-object pose solver."""
import argparse
import json

from multiworm_quality import sequence_guards, VERSION as GUARD_VERSION
from quality_schema import VERSION
from pipeline_progress import report_progress


def run(sample, mask_source=None, boundary_frames=None, *, root=None):
    from resource_paths import PROJECT_ROOT as ROOT
    from tracking_data import load_sample
    from serialization import json_safe
    root = root or ROOT
    base = root/'outputs'/sample
    config = json.loads((base/'pipeline-config.json').read_text(encoding='utf-8'))
    data, source, ids, get_mask, bad = load_sample(sample, mask_source or base/'sam2-tracking', apply_guards=False, root=root)
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
