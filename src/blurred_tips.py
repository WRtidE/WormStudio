"""Display-only localization of a moving tip with two resolved exposure traces."""
from dataclasses import dataclass

import cv2
import numpy as np

from skeleton_geometry import visible_terminal_branches


@dataclass
class ExposureTraces:
    tips: np.ndarray
    contrasts: np.ndarray  # Rows are detection thresholds; columns correspond to tips.
    darker_index: int | None


def trace_pair(dark, threshold, path, length, shaft_contrast):
    foreground=cv2.morphologyEx((dark>=threshold).astype('uint8'),cv2.MORPH_OPEN,np.ones((3,3),'uint8'))
    _,components=cv2.connectedComponents(foreground)
    x,y=np.rint(path[12]).astype(int)
    label=components[y,x]
    if not label:
        return None
    branches=visible_terminal_branches(components==label,length,.22*length)
    # Two distinct arms must leave the same distal fork, outward from the shaft.
    axis=path[12]-path[22]
    arms=[arm for arm in branches if np.linalg.norm(arm[0]-arm[-1])>=.035*length
          and np.linalg.norm(arm[-1]-path[12])<=.09*length and (arm[0]-arm[-1])@axis>0]
    if len(arms)!=2 or np.linalg.norm(arms[0][-1]-arms[1][-1])>.03*length:
        return None
    # Sharp bright-field contours can resemble two prongs. Exposure trails
    # diverge from the stem and each occupies only part of the exposure.
    vectors=np.array([arm[0]-arm[-1] for arm in arms])
    unit=vectors/np.linalg.norm(vectors,axis=1)[:,None]
    if unit[0]@unit[1]>.9:
        return None
    contrasts=[]
    for arm in arms:
        core=arm[len(arm)//4:3*len(arm)//4]
        contrast=np.median(dark[core[:,1],core[:,0]])
        if contrast>=.85*shaft_contrast:
            return None
        contrasts.append(contrast)
    tips=np.array([arm[0] for arm in arms])
    distances=np.linalg.norm(tips-path[0],axis=1)
    if distances.min()>.055*length or np.linalg.norm(tips[0]-tips[1])<max(6.,.035*length):
        return None
    if np.linalg.norm(tips[0]-tips[1])>.18*length:
        return None
    order=np.argsort(distances)
    return tips[order],np.asarray(contrasts)[order]


def exposure_traces(image, background, path, length, before, after):
    """Require repeatable branch ends and motion along their measured separation."""
    radius=max(1,round(.004*length))
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1))
    # A small registration envelope suppresses fixed pillar edges in the difference.
    dark=cv2.erode(background,kernel).astype('float32')-image
    dark-=np.median(dark)
    dark=cv2.GaussianBlur(dark,(5,5),1)
    xy=np.rint(path[12:20]).astype(int)
    contrast=float(np.median(dark[xy[:,1],xy[:,0]]))
    noise=float(np.median(np.abs(dark)))
    if contrast<max(12.,6*noise):
        return None
    threshold=max(4.,4*noise,.09*contrast)
    low=trace_pair(dark,threshold,path,length,contrast)
    if low is None:
        return None
    high=trace_pair(dark,1.25*threshold,path,length,contrast)
    if high is None or np.linalg.norm(low[0]-high[0],axis=1).max()>max(3.,.02*length):
        return None
    tips=(low[0]+high[0])*.5
    middle=tips.mean(axis=0)
    span=tips[1]-tips[0]
    separation=np.linalg.norm(span)
    motion=after-before
    direction=span/separation
    along=motion@direction
    if abs(along)<max(.85*np.linalg.norm(motion),separation):
        return None
    brackets=np.sort((np.stack((before,after))-middle)@direction)
    if brackets[0]>.2*separation or brackets[1]<-.2*separation:
        return None
    contrasts=np.stack((low[1],high[1]))
    darker=int(contrasts[0].argmax())
    # The same arm must win at both thresholds by more than local noise
    # and one gray level of quantization.
    if np.min(contrasts[:,darker]-contrasts[:,1-darker])<=max(1.,2*noise):
        darker=None
    return ExposureTraces(tips,contrasts,darker)


def display_exposure_tip(image, backgrounds, path, length, before, after, side):
    def detect(references):
        background=np.median(references,axis=0).astype('uint8')
        return exposure_traces(image,background,path,length,before,after)

    traces=detect(backgrounds)
    if traces is None:
        return None
    position=traces.tips.mean(axis=0)
    method='midpoint'
    if side=='tail' and traces.darker_index is not None and len(backgrounds)>=4:
        # Accept the visible tip only if leaving out any one background frame
        # independently selects the same physical tip. Branch order can change.
        tip=traces.tips[traces.darker_index]
        tolerance=min(max(3.,.02*length),.4*np.linalg.norm(traces.tips[1]-traces.tips[0]))
        for j in range(len(backgrounds)):
            other=detect(backgrounds[:j]+backgrounds[j+1:])
            if (other is None or other.darker_index is None
                    or np.linalg.norm(other.tips[other.darker_index]-tip)>tolerance):
                break
        else:
            position=tip
            method='darker_tip'
    return position,method,traces


def locate_blurred_tips(lines,facts,gray,masks,guards,excluded):
    """Attach display estimates to measured, oriented poses in a continuous identity run."""
    def usable(i):
        f=facts[i]
        return (f and f['head_known'] and not f['anchor_conflict'] and f['candidate']=='clear_observation'
                and f['head_observed'] and f['tail_observed'] and i not in excluded
                and (guards is None or (guards[i]['mask_valid'] and guards[i]['identity_valid']
                     and not guards[i].get('source_frame_invalid'))))

    references=[int(i) for i in np.unique(np.linspace(0,len(lines)-1,min(17,len(lines))).round().astype(int))
                if i not in excluded and (guards is None or (guards[i]['mask_valid'] and not guards[i].get('source_frame_invalid')))]
    height,width=gray[0].shape
    for i in range(1,len(lines)-1):
        f=facts[i]
        if not all(usable(j) for j in (i-1,i,i+1)):
            continue
        if (not all(facts[j]['component']==f['component'] for j in (i-1,i+1))
                or (guards is not None and not all(guards[j]['identity_segment']==guards[i]['identity_segment'] for j in (i-1,i+1)))):
            continue
        length=f['length_px']
        radius=int(np.ceil(.23*length))
        for side,order in (('head',1),('tail',-1)):
            path=lines[i,::order]
            end=0 if order==1 else -1
            before,after=lines[[i-1,i+1],end]
            if (np.linalg.norm(after-before)<max(6.,.035*length)
                    or np.linalg.norm(path[30:]-path[0],axis=1).min()<.20*length):
                continue
            origin=np.rint(path[0]).astype(int)-radius
            x,y=origin
            r,b=x+2*radius+1,y+2*radius+1
            if x<0 or y<0 or r>width or b>height:
                continue
            clean=[j for j in references if abs(j-i)>1 and not masks[j][y:b,x:r].any()]
            if len(clean)<3:
                continue
            selected=[clean[k] for k in np.linspace(0,len(clean)-1,min(5,len(clean))).round().astype(int)]
            backgrounds=[gray[j][y:b,x:r] for j in selected]
            found=display_exposure_tip(gray[i][y:b,x:r],backgrounds,path-origin,length,before-origin,after-origin,side)
            if found is not None:
                position,method,traces=found
                f.setdefault('blur_tip_estimates',{})[side]=dict(xy=(position+origin).tolist(),method=method,
                    trace_ends=(traces.tips+origin).tolist(),trace_contrasts=traces.contrasts.tolist(),
                    background_frames=selected,motion_frames=[i-1,i+1])
