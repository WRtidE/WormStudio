"""Offline image-based fragment association. Scores are rules, not probabilities."""
import copy
import math
from datetime import datetime
import cv2
import numpy as np

VERSION='source-image-reassociation-v3'
RULES=dict(minimum_span_s=.25, minimum_samples=3, window_frames=4,
    max_gap_s=1.5, area_ratio=[.65,1.55], width_ratio=[.6,1.65],
    max_appearance_distance=.35, max_displacement_body_lengths=1.5,
    minimum_foreground_support=.25, minimum_candidate_overlap=.3,
    foreground_threshold=12, candidate_area_ratio=[.25,2.5], minimum_elongation=1.6,
    minimum_unique_frames=3, background_samples=9, minimum_background_response=.1,
    maximum_background_shift_px=12.,minimum_background_std=5.)


def validate_reviews(config,ids,count):
    items=config.get('identity_reviews',[])
    if not isinstance(items,list):raise ValueError('identity_reviews 必须为列表')
    seen=set();bounds=set()
    for item in items:
        if not isinstance(item,dict):raise ValueError('片段复核记录无效')
        lo,hi,w=item.get('start_frame'),item.get('end_frame'),item.get('worm_id')
        if type(lo) is not int or type(hi) is not int or not 0<=lo<=hi<count or type(w) is not int or w not in ids:
            raise ValueError('片段复核帧范围或个体编号无效')
        if item.get('decision') not in ('same','different','unknown'):raise ValueError('片段复核意见无效')
        ref_lo,ref_hi=item.get('reference_start_frame'),item.get('reference_end_frame')
        if type(ref_lo) is not int or type(ref_hi) is not int or not 0<=ref_lo<=ref_hi<lo:
            raise ValueError('片段复核必须记录实际比较的前序片段范围')
        for key in ('id','reviewer','evidence','confirmed_at'):
            if not isinstance(item.get(key),str) or not item[key].strip():raise ValueError(f'片段复核缺少 {key}')
        if item['id'] in seen or (w,lo,hi) in bounds:raise ValueError('片段复核重复')
        try:
            if datetime.fromisoformat(item['confirmed_at']).utcoffset() is None:raise ValueError()
        except ValueError:raise ValueError('片段复核时间须包含时区') from None
        seen.add(item['id']);bounds.add((w,lo,hi))
    return {(r['worm_id'],r['start_frame'],r['end_frame']):r for r in items}


def chunks(records,fit_quality=None):
    result=[];current=[]
    for i,r in enumerate(records):
        good=r['mask_valid'] and not r.get('source_frame_invalid')
        if fit_quality is not None:good=good and fit_quality[i]['mask_valid']
        if current and (not good or r.get('identity_segment')!=records[current[-1]].get('identity_segment')):
            result.append(current);current=[]
        if good:current.append(i)
    if current:result.append(current)
    return result


class ImageEvidence:
    """Re-detect moving foreground independently of SAM masks, at half resolution.

    Low-contrast/stationary worms and registration failures intentionally abstain.
    Temporal median is evidence from the whole clip, not an online detector.
    """
    def __init__(self,source,get_mask):
        self.source=source;self.get_mask=get_mask;self.cap=cv2.VideoCapture(source['video'])
        if not self.cap.isOpened():raise ValueError('身份关联无法打开原视频')
        self.cache={};self.background=None

    def close(self):self.cap.release()

    def frame(self,f):
        if f not in self.cache:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES,f);ok,image=self.cap.read()
            if not ok:raise ValueError(f'身份关联无法读取源帧 {f}')
            x0,y0,x1,y1=self.source['crop_xyxy']
            image=cv2.cvtColor(image[y0:y1,x0:x1],cv2.COLOR_BGR2GRAY)
            self.cache[f]=cv2.resize(image,None,fx=.5,fy=.5,interpolation=cv2.INTER_AREA)
        return self.cache[f]

    def describe(self,w,f):
        image=self.frame(f)
        if self.background is None:
            sample=np.linspace(0,self.source['frames']-1,RULES['background_samples']).round().astype(int)
            self.background=np.median(np.stack([self.frame(int(j)) for j in sample]),axis=0).astype(np.uint8)
        mask=cv2.resize(self.get_mask(w,f).astype(np.uint8),image.shape[::-1],interpolation=cv2.INTER_NEAREST)>0
        area=float(mask.sum());dist=cv2.distanceTransform(mask.astype(np.uint8),cv2.DIST_L2,5)
        width=4*float(np.percentile(dist[mask],75)) if area else 0.
        # Only dark departures from the temporal background; static pillars disappear.
        residual=self.background.astype(float)-image.astype(float)
        foreground=(residual>=RULES['foreground_threshold']).astype(np.uint8)
        foreground=cv2.morphologyEx(foreground,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(foreground,8)
        candidates=[]
        for label in range(1,count):
            component=labels==label;a=int(stats[label,cv2.CC_STAT_AREA])
            if not RULES['candidate_area_ratio'][0]*area<=a<=RULES['candidate_area_ratio'][1]*area:continue
            yy,xx=np.nonzero(component);rect=cv2.minAreaRect(np.column_stack([xx,yy]).astype(np.float32))[1]
            elongation=max(rect)/max(1.,min(rect))
            if elongation<RULES['minimum_elongation']:continue
            # Search around the current target; unrelated distant objects are irrelevant.
            yy0,xx0=np.nonzero(mask)
            center=np.array([xx.mean(),yy.mean()]);target=np.array([xx0.mean(),yy0.mean()]) if area else center
            if np.linalg.norm(center-target)>max(40.,area/max(width,1.)):continue
            overlap=float((component&mask).sum()/max(1.,area))
            candidates.append(dict(area_px=a*4,overlap=overlap,elongation=float(elongation)))
        hist=np.histogram(image[mask],bins=16,range=(0,256))[0].astype(float);hist/=max(1,hist.sum())
        return dict(frame=f,area_px=area*4,width_px=width,histogram=hist.tolist(),
            centroid=np.mean(np.column_stack(np.nonzero(mask))[:,::-1],axis=0).tolist() if area else [0.,0.],
            foreground_support=float((foreground.astype(bool)&mask).sum()/max(1.,area)),
            candidates=candidates,candidate_count=len(candidates))

    def compare(self,w,before,after):
        a=[self.describe(w,int(f)) for f in before];b=[self.describe(w,int(f)) for f in after]
        def med(items,key):return float(np.median([x[key] for x in items]))
        area_ratio=med(b,'area_px')/max(1.,med(a,'area_px'))
        width_ratio=med(b,'width_px')/max(1.,med(a,'width_px'))
        appearance=float(np.abs(np.mean([r['histogram'] for r in a],axis=0)-np.mean([r['histogram'] for r in b],axis=0)).sum()/2)
        step=float(np.linalg.norm(np.array(a[-1]['centroid'])-b[0]['centroid'])*2)
        body=max(20.,med(a,'area_px')/max(1.,med(a,'width_px')))
        # Estimate source-wide shift after excluding tracked targets in both images.
        ia=self.frame(before[-1]).astype(np.float32);ib=self.frame(after[0]).astype(np.float32)
        ma=cv2.resize(self.get_mask(w,before[-1]).astype(np.uint8),ia.shape[::-1],interpolation=cv2.INTER_NEAREST)
        mb=cv2.resize(self.get_mask(w,after[0]).astype(np.uint8),ia.shape[::-1],interpolation=cv2.INTER_NEAREST)
        excluded=cv2.dilate(ma|mb,np.ones((21,21),np.uint8)).astype(bool)
        texture=min(float(ia[~excluded].std()),float(ib[~excluded].std()))
        ia[excluded]=0;ib[excluded]=0
        shift,response=cv2.phaseCorrelate(ia,ib)
        shift_px=float(np.linalg.norm(shift)*2)
        unique=sum(r['candidate_count']==1 and r['candidates'][0]['overlap']>=RULES['minimum_candidate_overlap']
            and r['foreground_support']>=RULES['minimum_foreground_support'] for r in b)
        checks=dict(area_match=RULES['area_ratio'][0]<=area_ratio<=RULES['area_ratio'][1],
            width_match=RULES['width_ratio'][0]<=width_ratio<=RULES['width_ratio'][1],
            appearance_match=appearance<=RULES['max_appearance_distance'],
            position_match=step/body<=RULES['max_displacement_body_lengths'],
            unique_redetection=unique>=min(RULES['minimum_unique_frames'],len(after)),
            background_consistent=texture>=RULES['minimum_background_std'] and response>=RULES['minimum_background_response'] and shift_px<=RULES['maximum_background_shift_px'])
        return dict(checks=checks,area_ratio=area_ratio,width_ratio=width_ratio,appearance_distance=appearance,
            centroid_step_px=step,displacement_body_lengths=step/body,unique_frames=unique,
            background_shift_px=shift_px,background_response=float(response),background_std=texture,before=a,after=b,
            accepted=all(checks.values()))


def reassociate(guards,source,get_mask,fit_qualities=None,reviews=(),evidence_provider=None):
    result=copy.deepcopy(guards);ids=sorted(guards);n=source['frames'];fps=source['fps']
    reviews=validate_reviews(dict(identity_reviews=list(reviews)),ids,n)
    minimum=max(RULES['minimum_samples'],math.ceil(RULES['minimum_span_s']*fps)+1)
    edges=[];provider=evidence_provider;owned=False;used=set()
    try:
        for w in ids:
            records=result[w];parts=chunks(records,(fit_qualities or {}).get(w));previous=None;manual_block=False
            for j,part in enumerate(parts):
                lo,hi=part[0],part[-1];key=(w,lo,hi);review=reviews.get(key)
                if review:used.add(key)
                stable=len(part)>=minimum
                initial=all(guards[w][f]['identity_valid'] for f in part) and not manual_block
                edge=dict(id=f'w{w}-f{lo}-{hi}',worm_id=w,start_frame=lo,end_frame=hi,stable=stable,
                    reference_start_frame=previous[0] if previous else None,
                    reference_end_frame=previous[-1] if previous else None,
                    decision='seed_continuous' if initial else 'unresolved',accepted=initial,
                    evidence=None,review=review,human_confirmed=initial and all(guards[w][f]['identity_status']=='reviewed_continuous' for f in part),reason='initial_provisional_continuity' if initial else 'insufficient_evidence')
                if review:
                    reference_matches=previous is not None and (review['reference_start_frame'],review['reference_end_frame'])==(previous[0],previous[-1])
                    if not reference_matches:
                        edge.update(decision='stale_review',accepted=False,human_confirmed=False,reason='reference_fragment_changed')
                        manual_block=True
                    else:
                        edge.update(decision='human_'+review['decision'],accepted=review['decision']=='same',
                            human_confirmed=review['decision']=='same',reason='explicit_fragment_review')
                        manual_block=review['decision']!='same'
                elif not initial:
                    if not stable:edge['reason']='short_fragment'
                    elif manual_block:edge['reason']='previous_human_split_or_unknown'
                    elif previous is None:edge['reason']='no_accepted_reference'
                    elif len(ids)!=1:edge['reason']='automatic_multiobject_association_disabled'
                    elif (lo-previous[-1])/fps>RULES['max_gap_s']:edge['reason']='gap_too_long'
                    else:
                        if provider is None:provider=ImageEvidence(source,get_mask);owned=True
                        evidence=provider.compare(w,previous[-RULES['window_frames']:],part[:RULES['window_frames']])
                        edge.update(evidence=evidence,accepted=bool(evidence['accepted']),
                            decision='auto_reassociated' if evidence['accepted'] else 'ambiguous',
                            reason='image_evidence_passed' if evidence['accepted'] else 'image_evidence_conflict')
                if edge['accepted']:previous=part
                for f in part:
                    row=records[f];row['identity_segment']=1000+j
                    row['identity_original_valid']=guards[w][f]['identity_valid']
                    row.update(identity_link_id=edge['id'],identity_link_source=edge['decision'],identity_human_confirmed=edge['human_confirmed'])
                    if edge['accepted']:
                        row.update(identity_valid=True,identity_review=False,identity_reason='',
                            identity_status='reviewed_continuous' if edge['human_confirmed'] else 'auto_reassociated' if edge['decision']=='auto_reassociated' else row['identity_status'],
                            identity_evidence=dict(link_id=edge['id'],source=edge['decision'],review=review,
                                original_guard_evidence=guards[w][f].get('identity_evidence')))
                    else:
                        row.update(identity_valid=False,identity_review=True,identity_reason='identity_unverified',
                            identity_status='fragment_review_required')
                edges.append(edge)
        if set(reviews)!=used:raise ValueError('片段边界已变化，原身份复核记录不再适用；请重新核对')
    finally:
        if owned:provider.close()
    return result,dict(version=VERSION,rules=RULES,scope='algorithmic association, not biological ground truth',
        scores_are_probabilities=False,edges=edges)
