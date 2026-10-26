"""Repair cached WormTracer fits using branch candidates and sequence selection.

Run on all supplied videos, retaining upstream results in their original folders.
"""
import argparse
from pipeline_progress import report_progress
import json
import time
from pathlib import Path
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from scipy import ndimage as ndi
from analyze_phenotypes import ROOT,load_sample
from centerline_topology import topology_candidates,geometry_quality,resample
from pose_segments import accepted_segments, rejected_quality


def local_field(mask):
    ys,xs=np.nonzero(mask);x=max(0,int(xs.min())-8);y=max(0,int(ys.min())-8)
    local=mask[y:int(ys.max())+9,x:int(xs.max())+9]
    return ndi.distance_transform_edt(local),np.array([x,y])


def candidate_score(candidate,mask,field,origin,length_ref,radius,tips):
    p=candidate['points'];q=p-origin
    inside=ndi.map_coordinates(mask.astype(np.uint8),[p[:,1],p[:,0]],order=0,mode='constant').mean()
    length=float(np.linalg.norm(np.diff(p,axis=0),axis=1).sum())
    qa=geometry_quality(p)
    tipcost=0
    if tips is not None:
        tipcost=min(np.linalg.norm(p[[0,-1]]-tips,axis=1).mean(),np.linalg.norm(p[[0,-1]]-tips[::-1],axis=1).mean())/radius
    # Penalize missed body coverage, not projected self-intersection.
    radius_points=ndi.map_coordinates(field,[q[:,1],q[:,0]],order=1,mode='constant')
    reconstruction=np.zeros(field.shape,np.uint8)
    for xy,r in zip(q,radius_points):
        cv2.circle(reconstruction,tuple(np.rint(xy).astype(int)),max(1,int(round(min(r,1.3*radius)))),1,-1)
    truth=field>0;intersection=(truth&(reconstruction>0)).sum();union=(truth|(reconstruction>0)).sum()
    iou=float(intersection/max(union,1))
    unary=(12*(1-inside)+3*(1-iou)+4*abs(length/length_ref-1)
           +.5*min((qa['max_turn_5pct_deg']/90)**2,16)+2*candidate['connection_cost']
           +.8*min(tipcost,5))
    candidate.update(unary=float(unary),inside_fraction=float(inside),reconstruction_iou=iou,
                     length_px=length,**qa)


def sequence_select(frames,length_ref,bad):
    """Viterbi plus backward costs use both earlier and later frame evidence."""
    states=[]
    for candidates in frames:
        state=[]
        for k,c in enumerate(candidates):
            for reverse in (False,True):
                state.append(dict(candidate=k,reverse=reverse,points=c['points'][::-1] if reverse else c['points'],unary=c['unary']))
        states.append(state)
    def transition(left,right,reset):
        if reset:return np.zeros((len(left),len(right)))
        a=np.array([s['points'] for s in left]);b=np.array([s['points'] for s in right])
        a=a-a.mean(axis=1,keepdims=True);b=b-b.mean(axis=1,keepdims=True)
        diff=a[:,None]-b[None,:]
        shape=np.mean(np.sum(diff**2,axis=-1),axis=-1)
        ends=np.mean(np.sum(diff[:,:,[0,-1],:]**2,axis=-1),axis=-1)
        return (shape+.5*ends)/(length_ref*.1)**2
    trans=[transition(states[i-1],states[i],bad[i] or bad[i-1]) for i in range(1,len(states))]
    forward=[np.array([s['unary'] for s in states[0]])];backpointers=[]
    # A/B order is arbitrary, anchored to the first selected candidate's order.
    forward[0][1::2]+=1000
    for i in range(1,len(states)):
        costs=forward[-1][:,None]+trans[i-1]
        backpointers.append(costs.argmin(axis=0))
        forward.append(costs.min(axis=0)+np.array([s['unary'] for s in states[i]]))
    chosen=[int(forward[-1].argmin())]
    for i in range(len(states)-2,-1,-1):chosen.append(int(backpointers[i][chosen[-1]]))
    chosen=chosen[::-1]
    backward=[None]*len(states);backward[-1]=np.zeros(len(states[-1]))
    for i in range(len(states)-2,-1,-1):
        nextcost=np.array([s['unary'] for s in states[i+1]])+backward[i+1]
        backward[i]=(trans[i]+nextcost[None,:]).min(axis=1)
    selected=[];margins=[]
    for i,index in enumerate(chosen):
        s=states[i][index];selected.append(s)
        topology=frames[i][s['candidate']]['projection_crossings']>0
        alternatives=[forward[i][j]+backward[i][j] for j,t in enumerate(states[i])
                      if (frames[i][t['candidate']]['projection_crossings']>0)!=topology]
        best=forward[i][index]+backward[i][index]
        margins.append(float(min(alternatives)-best) if alternatives else None)
    return selected,margins


def refine_on_gpu(points,fields,origins,zones,kinds):
    """Subpixel normal refinement; freeze tips, contact bridges and unresolved fits.

    Optimizes medial balance at native resolution. It cannot change branch
    pairing; that discrete decision is made by the sequence optimizer above.
    """
    if not torch.cuda.is_available():raise RuntimeError('CUDA required')
    result=points.copy();device='cuda'
    for start in range(0,len(points),24):
        end=min(start+24,len(points));batch=points[start:end];sub=fields[start:end]
        h=max(d.shape[0] for d in sub);w=max(d.shape[1] for d in sub)
        array=np.zeros((len(sub),1,h,w),np.float32)
        for k,d in enumerate(sub):array[k,0,:d.shape[0],:d.shape[1]]=d
        data=torch.as_tensor(array,device=device)
        base=torch.tensor(batch-np.array(origins[start:end])[:,None,:],dtype=torch.float32,device=device)
        tangent=np.gradient(batch,axis=1);normal=np.stack([-tangent[:,:,1],tangent[:,:,0]],axis=-1)
        normal/=np.maximum(np.linalg.norm(normal,axis=-1,keepdims=True),1e-6)
        normal=torch.tensor(normal,dtype=torch.float32,device=device)
        allowed=np.ones((len(sub),101),np.float32);allowed[:,:4]=0;allowed[:,-4:]=0
        for k,p in enumerate(batch):
            if kinds[start+k]=='unresolved_contact':allowed[k]=0
            z=zones[start+k]
            contact=ndi.map_coordinates(z.astype(np.uint8),[p[:,1],p[:,0]],order=0,mode='constant')>0
            allowed[k,ndi.binary_dilation(contact,iterations=3)]=0
        active=torch.tensor(allowed,device=device)
        offset=torch.zeros((len(sub),101),device=device,requires_grad=True)
        opt=torch.optim.Adam([offset],lr=.12)
        def sample(p):
            grid=p/torch.tensor([w-1,h-1],device=device)*2-1
            return F.grid_sample(data,grid[:,:,None,:],align_corners=True,padding_mode='zeros')[:,0,:,0]
        for _ in range(70):
            shift=3*torch.tanh(offset)*active
            p=base+shift[:,:,None]*normal
            balance=(sample(p+2*normal)-sample(p-2*normal))/4
            loss=(balance.square()*active).sum()/active.sum().clamp_min(1)
            loss=loss+.025*shift.square().mean()+.04*(shift[:,1:]-shift[:,:-1]).square().mean()
            opt.zero_grad();loss.backward();opt.step()
        result[start:end]=batch+(3*torch.tanh(offset)*active)[:,:,None].detach().cpu().numpy()*normal.cpu().numpy()
    return np.array([resample(p) for p in result])


def run(sample,source_name,output_name,worm_id=None):
    started=time.time();mask_source,source,ids,get_mask,tracking_bad=load_sample(sample)
    for worm in ids:
        if worm_id is not None and worm!=worm_id:continue
        print(f'{sample} worm {worm}: generating native-pixel branch candidates',flush=True)
        old=np.load(ROOT/'outputs'/sample/source_name/f'worm-{worm}'/'centerlines.npy')
        segments=accepted_segments(source['frames'],worm,tracking_bad,old)
        accepted={int(f) for segment in segments for f in segment}
        frames=[];infos=[];zones=[];fields=[];origins=[];masks=[];simple_lengths=[]
        for f in range(source['frames']):
            report_progress(f,source['frames'],'生成交叉分支候选',fraction=.45*f/source['frames'])
            if f not in accepted:
                frames.append([]);infos.append(None);zones.append(None);fields.append(None);origins.append(None);masks.append(None)
                continue
            mask=get_mask(worm,f);c,zone,info=topology_candidates(mask)
            if info['kind']=='simple':simple_lengths.append(np.linalg.norm(np.diff(c[0]['points'],axis=0),axis=1).sum())
            c.append(dict(points=old[f],name='wormtracer_original',connection_cost=0.0))
            field,origin=local_field(mask)
            frames.append(c);infos.append(info);zones.append(zone);fields.append(field);origins.append(origin);masks.append(mask)
        reference_lengths=simple_lengths or [np.linalg.norm(np.diff(old[f],axis=0),axis=1).sum() for f in accepted]
        ref=float(np.median(reference_lengths)) if reference_lengths else 1.
        for f,candidates in enumerate(frames):
            if f not in accepted:continue
            report_progress(f,len(frames),'评估分支与虫体轮廓支持',fraction=.45+.15*f/len(frames))
            tips=candidates[0]['points'][[0,-1]] if len(candidates)>1 else None
            for c in candidates:candidate_score(c,masks[f],fields[f],origins[f],ref,infos[f]['radius_px'],tips)
        bad=[f not in accepted for f in range(source['frames'])]
        selected=[None]*len(old);margins=[None]*len(old);points=np.full_like(old,np.nan)
        report_progress(detail='CUDA 中线微调',fraction=.6,force=True)
        for segment in segments:
            chosen,confidence=sequence_select([frames[f] for f in segment],ref,[False]*len(segment))
            refined=refine_on_gpu(np.array([s['points'] for s in chosen]),[fields[f] for f in segment],
                [origins[f] for f in segment],[zones[f] for f in segment],[infos[f]['kind'] for f in segment])
            points[segment]=refined
            for f,s,margin in zip(segment,chosen,confidence):selected[f]=s;margins[f]=margin
        quality=rejected_quality(source['frames'])
        for f,(p,s,info) in enumerate(zip(points,selected,infos)):
            if f not in accepted:continue
            report_progress(f,len(points),'检查修正后的中线质量',fraction=.7+.29*f/len(points))
            c=frames[f][s['candidate']];qa=geometry_quality(p);reasons=[]
            inside=float(ndi.map_coordinates(masks[f].astype(np.uint8),[p[:,1],p[:,0]],order=0,mode='constant').mean())
            if info['kind']=='unresolved_contact':reasons.append('ambiguous_self_contact')
            if info['kind']=='paired_contact' and (margins[f] is None or margins[f]<.5):reasons.append('ambiguous_branch_pairing')
            if qa['local_hairpin']:reasons.append('local_hairpin')
            if inside<.97:reasons.append('centerline_outside_mask')
            length=float(np.linalg.norm(np.diff(p,axis=0),axis=1).sum())
            if not .8*ref<length<1.2*ref:reasons.append('length_outlier')
            if bad[f]:reasons.append('tracking_contact_or_area_outlier')
            quality[f]=dict(frame=f,selected_candidate=c['name'],topology=info['kind'],
                                branch_score_margin=margins[f],mask_valid=not bad[f],
                                centerline_valid=not reasons,head_identity_confirmed=False,
                                quality_reasons=reasons,inside_fraction=inside,**qa)
        target=ROOT/'outputs'/sample/output_name/f'worm-{worm}';target.mkdir(parents=True,exist_ok=True)
        np.save(target/'centerlines.npy',points)
        (target/'quality.json').write_text(json.dumps(quality,indent=2),encoding='utf-8')
        details=[[{k:v for k,v in c.items() if k!='points'} for c in candidates] for candidates in frames]
        (target/'candidates.json').write_text(json.dumps(details,indent=2),encoding='utf-8')
        record=dict(algorithm='WormTracer + native-pixel branch candidates + bidirectional sequence selection + CUDA medial refinement',
                    source=str(ROOT/'outputs'/sample/source_name/f'worm-{worm}'),mask_source=str(mask_source),
                    frames=source['frames'],reference_length_px=ref,seconds_since_sample_start=time.time()-started,
                    device=torch.cuda.get_device_name(),endpoints='A/B arbitrary; no anatomical head inference',
                    valid_frames=sum(q['centerline_valid'] for q in quality),
                    thresholds=dict(hairpin_5pct_deg=150,inside_fraction=.97,branch_score_margin=.5,length_fraction=[.8,1.2]),
                    threshold_status='operational; no independent annotated validation set')
        (target/'run.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        print(f'{sample} worm {worm}: saved {len(points)} lines, {record["valid_frames"]} quality-passed',flush=True)
        for f in (97,132):
            if worm==2 and f<len(points):print(quality[f],flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--sample',default='all')
    p.add_argument('--source',default='wormtracer-tuned');p.add_argument('--output-name',default='wormtracer-corrected')
    p.add_argument('--worm',type=int,help='Process one object independently')
    args=p.parse_args()
    for sample in (['2-1-1314','5-1-910'] if args.sample=='all' else [args.sample]):run(sample,args.source,args.output_name,args.worm)
