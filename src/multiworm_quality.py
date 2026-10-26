"""Conservative multi-object guards; never recover inter-worm contact as self-contact."""
import cv2
import numpy as np

VERSION = 'multiworm-contact-identity-guards-v4-reviewed-reanchors'


def validate_reviews(config, ids, count):
    """Require explicit, auditable reviews; a SAM ID or head point is not evidence."""
    from datetime import datetime
    excluded = config.get('source_invalid_frames', [])
    if not isinstance(excluded, list) or any(type(f) is not int or not 0 <= f < count for f in excluded):
        raise ValueError('source_invalid_frames 必须是视频内的整数帧号列表')
    events = config.get('identity_confirmations', [])
    if not isinstance(events, list): raise ValueError('identity_confirmations 必须是列表')
    seen = set()
    for event in events:
        if not isinstance(event, dict): raise ValueError('身份确认必须包含帧号、虫编号、复核人、时间和独立证据')
        key = (event.get('frame'), event.get('worm_id'))
        if (type(key[0]) is not int or not 0 <= key[0] < count or type(key[1]) is not int
                or key[1] not in ids or key in seen):
            raise ValueError('身份确认帧或虫编号无效、重复')
        for name in ('reviewer', 'evidence', 'confirmed_at'):
            if not isinstance(event.get(name), str) or not event[name].strip():
                raise ValueError(f'身份确认缺少 {name}；不能以编号连续代替独立复核')
        try:
            if datetime.fromisoformat(event['confirmed_at']).utcoffset() is None: raise ValueError()
        except ValueError:
            raise ValueError('confirmed_at 必须是包含时区的 ISO 时间') from None
        seen.add(key)
    return {(e['frame'], e['worm_id']): dict(e) for e in events}


def frame_geometry(masks, proximity_px=2):
    """Return per-object contact/fragmentation/border evidence for one frame.

    A 2 px contact margin accommodates mask rasterization. IDs are provisional
    SAM2 object IDs; these engineering checks do not establish true identity.
    """
    ids = sorted(masks)
    masks = {w: np.asarray(masks[w], bool) for w in ids}
    shape = masks[ids[0]].shape
    assert all(m.shape == shape for m in masks.values())
    result = {}
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                     (2*proximity_px+1, 2*proximity_px+1))
    dilated = {}
    for w, mask in masks.items():
        area = int(mask.sum())
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        pieces = sum(a >= max(20, .05*area) for a in stats[1:, cv2.CC_STAT_AREA])
        border = int(mask[0].sum()+mask[-1].sum()+mask[:,0].sum()+mask[:,-1].sum())
        yy, xx = np.nonzero(mask)
        result[w] = dict(contact_ids=[], overlap_pixels=0, fragmented=bool(pieces>1),
                         boundary_truncated=bool(border>=3), area_px=area,
                         centroid=[float(xx.mean()),float(yy.mean())] if area else [np.nan,np.nan])
        dilated[w] = cv2.dilate(mask.astype(np.uint8), kernel)>0
    for j, a in enumerate(ids):
        for b in ids[j+1:]:
            overlap = int((masks[a]&masks[b]).sum())
            near = int((dilated[a]&masks[b]).sum())
            if overlap>=3 or near>=3:
                result[a]['contact_ids'].append(b)
                result[b]['contact_ids'].append(a)
            result[a]['overlap_pixels'] += overlap
            result[b]['overlap_pixels'] += overlap
    return result


def sequence_guards(all_masks, original_bad=None, source_invalid=(), fps=30., identity_confirmations=()):
    """Mark contact and keep identity unverified after an interaction.

    Post-contact geometries may still be displayed as candidates; per-worm
    quantitative claims require an independent identity re-anchor, which this
    code does not fabricate. Reviews apply forward only and never relabel IDs.
    """
    ids=sorted(all_masks);n=len(all_masks[ids[0]])
    assert np.isfinite(fps) and fps>0
    assert all(len(m)==n for m in all_masks.values())
    original_bad=original_bad or {};source_invalid=set(source_invalid)
    records={w:[] for w in ids};uncertain={w:False for w in ids}
    confirmations=validate_reviews(dict(identity_confirmations=list(identity_confirmations),
        source_invalid_frames=list(source_invalid)), ids, n)
    segments={w:0 for w in ids};evidence={w:'Initial seed + provisional SAM2 continuity' for w in ids}
    references={w:max(1.,float(np.median([m.sum() for m in all_masks[w] if m.any()]))) for w in ids}
    previous={}
    for i in range(n):
        current=frame_geometry({w:all_masks[w][i] for w in ids})
        for w,r in current.items():
            input_bad=i in source_invalid
            if input_bad:r['contact_ids']=[]
            trusted=bool(not input_bad and not original_bad.get((i,w),False) and r['area_px']
                         and not r['fragmented'] and not r['boundary_truncated'] and not r['contact_ids'])
            jump=float(np.linalg.norm(np.asarray(r['centroid'])-previous[w][0])) if w in previous and trusted else np.nan
            gap=max(1,i-previous[w][1]) if w in previous else 1
            # Preserve the previous 30 fps calibration as a speed limit, so
            # identical physical motion is not penalized at a lower frame rate.
            speed=jump/gap*fps
            jump_review=bool(np.isfinite(speed) and speed>30.*max(20.,.4*np.sqrt(references[w])))
            if (r['contact_ids'] and not input_bad) or jump_review:
                if not uncertain[w]:segments[w]+=1
                uncertain[w]=True
                evidence[w]=dict(event_frame=i,contact_ids=r['contact_ids'],centroid_step_speed_px_s=speed if np.isfinite(speed) else None)
            if trusted:
                previous[w]=(np.array(r['centroid']),i)
            reasons=[]
            if input_bad:reasons.append('invalid_decoded_source_frame')
            if r['contact_ids']:reasons.append('inter_worm_contact')
            if r['fragmented']:reasons.append('fragmented_mask')
            if r['boundary_truncated']:reasons.append('image_boundary_truncation')
            if original_bad.get((i,w),False):reasons.append('sam2_tracking_quality')
            if not r['area_px']:reasons.append('missing_mask')
            if jump_review:reasons.append('centroid_jump')
            confirmation=confirmations.get((i,w))
            if confirmation:
                if reasons:raise ValueError(f'身份确认帧 {i} / 虫 {w} 未通过源帧或掩膜检查，不能恢复身份')
                uncertain[w]=False;segments[w]+=1;evidence[w]=confirmation
            identity=('source_frame_excluded' if input_bad else 'contact_ambiguous' if r['contact_ids'] else
                      'jump_review' if jump_review else
                      'post_contact_or_jump_unverified' if uncertain[w] else
                      'reviewed_continuous' if isinstance(evidence[w],dict) and 'reviewer' in evidence[w] else 'provisional_continuous')
            r.update(frame=i,worm_id=w,mask_valid=not reasons,
                     reasons=reasons,identity_status=identity,
                     identity_valid=not uncertain[w] and not input_bad,
                     identity_reason='identity_unverified' if uncertain[w] else 'source_frame_excluded' if input_bad else '',
                     identity_evidence=evidence[w],identity_segment=segments[w],
                     identity_review=uncertain[w],centroid_step_px=jump,
                     centroid_step_speed_px_s=speed,
                     source_frame_invalid=input_bad)
            records[w].append(r)
    return records
