"""Replay a crop-boundary incident from an adjacent reliable SAM2 mask."""
import argparse
import csv
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from quality.multiworm_quality import frame_geometry
from pipeline_progress import report_progress
from quality.tracking_quality import mark_quality

VERSION = 'sam2-crop-replay-v1'
FOLDER = 'crop-recovery'
SIDES = ('left', 'top', 'right', 'bottom')


def tracking_directory(base, config):
    raw = base / 'sam2-tracking'
    recovered = raw / FOLDER
    if config.get('centerline_method') == 'self_contact' and (recovered / 'summary.json').is_file():
        return recovered
    return raw


def place(mask, box, canvas):
    """Move a mask between two source-coordinate rectangles, clipping their intersection."""
    if mask.shape != (box[3]-box[1], box[2]-box[0]):
        raise ValueError('掩膜尺寸与源视频坐标不符')
    if box == canvas:
        return mask
    x, y = max(box[0], canvas[0]), max(box[1], canvas[1])
    r, b = min(box[2], canvas[2]), min(box[3], canvas[3])
    result = np.zeros((canvas[3]-canvas[1], canvas[2]-canvas[0]), mask.dtype)
    if x < r and y < b:
        result[y-canvas[1]:b-canvas[1], x-canvas[0]:r-canvas[0]] = mask[y-box[1]:b-box[1], x-box[0]:r-box[0]]
    return result


def touched(mask):
    counts = [int(np.count_nonzero(v)) for v in (mask[:,0], mask[0], mask[:,-1], mask[-1])]
    return [i for i, count in enumerate(counts) if count] if sum(counts) >= 3 else []


def contiguous(frames):
    spans = []
    for f in sorted(frames):
        if not spans or f != spans[-1][-1] + 1:
            spans.append([])
        spans[-1].append(f)
    return spans


def replay_model(config):
    import torch
    from platforms.device import selected_device, inference_context
    from sam2.build_sam import build_sam2_video_predictor
    from runtime.resource_paths import sam2_checkpoint
    from tracking.sam2_models import model_spec
    from tracking.sam2_frames import init_tracking_state, propagate_frames
    from tracking.track_configured_video import sam2_input_transform
    spec = model_spec(config.get('sam2_model', 'large'))
    device = selected_device()
    model = build_sam2_video_predictor(spec['config'], str(sam2_checkpoint(spec['checkpoint'])), device=device,
        hydra_overrides_extra=['++model.non_overlap_masks=false','++model.non_overlap_masks_for_mem_enc=false'])
    transform = sam2_input_transform(config.get('sam2_input_contrast', 'none'))

    def predict(cap, box, indices, seed):
        x,y,r,b = box
        with TemporaryDirectory(prefix='worm-crop-replay-') as tmp, torch.inference_mode(), inference_context(device):
            directory = Path(tmp)
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(indices))
            for f in range(min(indices), max(indices)+1):
                ok, im = cap.read()
                if not ok:
                    raise ValueError(f'扩框复核无法读取源帧 {f}')
                j = abs(f-indices[0])
                image = transform(im[y:b,x:r])
                ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 94])
                if not ok:
                    raise OSError('无法保存扩框复核临时帧')
                (directory / f'{j:06}.jpg').write_bytes(jpeg.tobytes())
            state = init_tracking_state(model, directory, len(indices))
            model.add_new_mask(state, frame_idx=0, obj_id=1, mask=seed)
            for j, _, logits in propagate_frames(model, state, start_frame_idx=0):
                if j:
                    mask = (logits[0,0].cpu().numpy() > 0).astype(np.uint8)
                    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
                    keep = stats[:, cv2.CC_STAT_AREA] >= 20
                    keep[0] = False
                    yield indices[j], keep[labels]
    return predict


def same_mask(a, b):
    shared = int(np.count_nonzero(a & b))
    return shared / max(1, int(np.count_nonzero(a | b)))


def incidents(hits, records):
    """An empty detection immediately after a crop exit belongs to the same incident."""
    missing = {r['frame'] for r in records if not r['area_px'] and not r['source_frame_invalid']}
    return [span for span in contiguous(set(hits) | missing) if any(f in hits for f in span)]


def replay_incident(predictor, cap, expanded, box, original, indices, span, w, ids, get, stage):
    from analyze_phenotypes import write_png
    seed = place(get(w,indices[0]), original, expanded)
    _, _, width, height = cv2.boundingRect(seed.astype(np.uint8))
    ref_area = int(seed.sum())
    previous = seed
    pending = {}
    stream = predictor(cap, expanded, indices, seed)
    try:
        for f, mask in stream:
            geometry = frame_geometry({w:mask})[w]
            if touched(mask) or geometry['fragmented'] or not .75*ref_area <= geometry['area_px'] <= 1.25*ref_area:
                return {}, 'replay_shape_rejected'
            old = place(get(w,f), original, box)
            visible = place(mask, expanded, box)
            if old.any():
                if same_mask(visible, old) < (.40 if f in span else .65):
                    return {}, 'track_disagreement'
            elif visible.sum() > .15*ref_area or same_mask(previous, mask) < .25:
                # With no old mask require a consistent target crossing out of the old ROI.
                return {}, 'unsupported_exit'
            if len(ids) > 1:
                near = cv2.dilate(mask.astype(np.uint8), np.ones((5,5),np.uint8)).astype(bool)
                if any(np.any(near & place(get(v,f),original,expanded)) for v in ids if v!=w):
                    return {}, 'other_worm_contact'
            yy, xx = np.nonzero(previous)
            if np.linalg.norm(np.array(geometry['centroid'])-[xx.mean(),yy.mean()]) > max(width,height)*.65:
                return {}, 'replay_jump'
            if f in span:
                path = stage/f'w{w}-f{f}.png'
                write_png(path,mask.astype(np.uint8)*255)
                pending[w,f] = (expanded,path)
            previous = mask
    finally:
        if hasattr(stream,'close'):
            stream.close()
    if len(pending) != len(span):
        raise ValueError('扩框重跟踪帧数不完整')
    return pending, 'recovered'


def save_masks(dest, raw, meta, config, ids, get, patches, final_box):
    from analyze_phenotypes import read_image, write_png
    original, count = meta['crop_xyxy'], meta['frames']
    metrics = []
    for f in range(count):
        masks = {}
        for w in ids:
            target = dest/'masks'/f'worm-{w}'/f'{f:06}.png'
            target.parent.mkdir(parents=True,exist_ok=True)
            if (w,f) in patches:
                box, p = patches[w,f]
                masks[w] = place(read_image(p, gray=True) > 0, box, final_box)
            else:
                masks[w] = place(get(w,f),original,final_box)
            if final_box==original and (w,f) not in patches:
                shutil.copyfile(raw/'masks'/f'worm-{w}'/target.name,target)
            else:
                write_png(target, masks[w].astype(np.uint8)*255)
        for w,m in masks.items():
            yy,xx = np.nonzero(m)
            overlap=np.zeros_like(m)
            for v in ids:
                if w!=v:
                    overlap |= m & masks[v]
            metrics.append(dict(frame=f,worm_id=w,area_px=len(yy),centroid_x_px=float(xx.mean()) if len(xx) else None,
                centroid_y_px=float(yy.mean()) if len(yy) else None,ambiguous_overlap_px=int(overlap.sum())))
    mark_quality(metrics,ids,config.get('review_intervals',[]))
    with (dest/'tracking-quality.csv').open('w',newline='') as fp:
        writer=csv.DictWriter(fp,fieldnames=list(metrics[0]))
        writer.writeheader()
        writer.writerows(metrics)


def run(sample, predictor=None):
    from analyze_phenotypes import ROOT, load_sample
    from quality.quality_stage import load_guards, run as guard_stage
    base = ROOT/'outputs'/sample
    config = json.loads((base/'pipeline-config.json').read_text(encoding='utf-8-sig'))
    raw, meta, ids, get, _ = load_sample(sample, base/'sam2-tracking', apply_guards=False)
    count = meta['frames']
    original = meta['crop_xyxy']
    meta.pop('mask_source', None)
    guards = load_guards(raw, ids, count, required=True)
    cap = cv2.VideoCapture(config['video'])
    if not cap.isOpened():
        raise ValueError('扩框复核无法打开源视频')
    sensor = [0,0,int(cap.get(3)),int(cap.get(4))]
    final_box = original.copy()
    events = []
    unresolved = {w:set() for w in ids}
    with TemporaryDirectory(dir=raw, prefix='.crop-replay-') as tmp:
        stage = Path(tmp)
        # Store accepted masks at their native coordinates until the final canvas is known.
        patches = {}
        try:
            for group in config['groups']:
                box = group['crop_xyxy']
                for worm in group['worms']:
                    w = worm['id']
                    hits = {}
                    for f in range(count):
                        sides = touched(place(get(w,f), original, box))
                        if sides:
                            hits[f] = sides
                    unresolved[w].update(hits)
                    reviewed = guards[w]
                    refs_checked = False
                    protected = {group['anchor_frame']}
                    protected.update(a['frame'] for a in group.get('additional_anchors', []) if any(v['id']==w for v in a['worms']))
                    protected.update(a['frame'] for a in config.get('identity_confirmations', []) if a['worm_id']==w)
                    for span in incidents(hits, guards[w]):
                        first,last = span[0],span[-1]
                        sides = sorted({side for f in span for side in hits.get(f, ())})
                        edge = dict(worm_id=w, first=first, last=last, sides=[SIDES[i] for i in sides])
                        events.append(edge)
                        if any(box[i] == sensor[i] for i in sides):
                            edge['decision'] = 'source_boundary'
                            continue
                        excluded = any(w in r['worm_ids'] and r['frames'][0]<=last and first<=r['frames'][1] for r in config.get('review_intervals', []))
                        if (excluded or protected.intersection(span) or any(guards[w][f]['source_frame_invalid'] or guards[w][f]['contact_ids'] for f in span)
                                or any(r['worm_id']==w and r['reference_start_frame']<=last and first<=r['end_frame'] for r in config.get('identity_reviews', []))):
                            edge['decision'] = 'protected_or_contact'
                            continue
                        neighbours = [f for f in (first-1,last+1) if 0 <= f < count]
                        if (not refs_checked and len(ids)==1 and (config.get('identity_reviews') or any(not reviewed[f]['identity_valid'] for f in neighbours))):
                            from quality.identity_reassociation import reassociate
                            linked, _ = reassociate({w:guards[w]}, meta, get, reviews=config.get('identity_reviews', []))
                            reviewed = linked[w]
                            refs_checked = True
                        seed_frame = next((f for f in neighbours if reviewed[f]['mask_valid'] and reviewed[f]['identity_valid']), None)
                        if seed_frame is None:
                            edge['decision'] = 'no_reliable_seed'
                            continue
                        step = 1 if seed_frame < first else -1
                        indices = [seed_frame, *(span if step==1 else reversed(span))]
                        check = indices[-1] + step
                        check_join = 0 <= check < count and guards[w][check]['mask_valid']
                        if check_join:
                            indices.append(check)
                        seed_old = get(w,seed_frame)
                        _,_,sw,sh = cv2.boundingRect(seed_old.astype(np.uint8))
                        margin = max(32, 2*max(sw,sh))
                        expanded = box.copy()
                        for side in sides:
                            expanded[side] = max(sensor[side],box[side]-margin) if side<2 else min(sensor[side],box[side]+margin)
                        edge.update(seed=seed_frame, crop_xyxy=expanded)
                        if predictor is None:
                            predictor = replay_model(config)
                        report_progress(detail=f'从第 {seed_frame} 帧可靠掩膜扩框复核 W{w} · {first}–{last}',force=True)
                        pending, decision = replay_incident(predictor, cap, expanded, box, original, indices, set(span), w, ids, get, stage)
                        edge['decision'] = decision
                        if decision == 'recovered':
                            patches.update(pending)
                            unresolved[w].difference_update(span)
                            final_box = [min(final_box[0],expanded[0]),min(final_box[1],expanded[1]),
                                         max(final_box[2],expanded[2]),max(final_box[3],expanded[3])]
        finally:
            cap.release()
        dest = stage/'result'
        dest.mkdir()
        if patches:
            save_masks(dest, raw, meta, config, ids, get, patches, final_box)
        else:
            shutil.copytree(raw/'masks',dest/'masks')
            shutil.copyfile(raw/'tracking-quality.csv',dest/'tracking-quality.csv')
        report = dict(version=VERSION, original_crop_xyxy=original, events=events, replaced_frames=len(patches))
        (dest/'summary.json').write_text(json.dumps(dict(meta,crop_xyxy=final_box,crop_recovery=report),ensure_ascii=False,indent=2), encoding='utf-8')
        if not patches and all(guards[w][f]['boundary_truncated'] for w in ids for f in unresolved[w]):
            shutil.copyfile(raw/'quality-guards.json',dest/'quality-guards.json')
        else:
            guard_stage(sample,mask_source=dest, boundary_frames=unresolved)
        # Publish only after every frame and both tables exist. A retry rebuilds from raw masks.
        target = raw/FOLDER
        if target.exists():
            target.rename(stage/'previous')
        try:
            dest.rename(target)
        except OSError:
            if (stage/'previous').exists():
                (stage/'previous').rename(target)
            raise
    print(f'扩框复核：{len(events)} 个触边区间，恢复 {len(patches)} 个虫帧',flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample',required=True)
    run(parser.parse_args().sample)
