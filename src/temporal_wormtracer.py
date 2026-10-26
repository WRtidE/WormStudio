"""Image-supported bidirectional motion estimates for self-contact transitions."""
import argparse
from pipeline_progress import report_progress
import json
import shutil
import time
import cv2
import numpy as np
import torch
from scipy import ndimage as ndi
from analyze_phenotypes import ROOT,load_sample
from centerline_topology import topology_candidates,hidden_tip_candidates,geometry_quality,resample
from repair_wormtracer import local_field,candidate_score


def align(p,reference):
    return p[::-1].copy() if np.mean((p[::-1]-reference)**2)<np.mean((p-reference)**2) else p.copy()


def lk_step(a,b,p,fallback_velocity=None):
    """Raw-image optical flow, with forward/backward consistency checks."""
    x=p.astype(np.float32).reshape(-1,1,2)
    def track(points,level):
        options=dict(winSize=(31,31),maxLevel=level,
                     criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,35,.01))
        nxt,status,error=cv2.calcOpticalFlowPyrLK(a,b,points,None,**options)
        back,status2,_=cv2.calcOpticalFlowPyrLK(b,a,nxt,None,**options)
        fb=np.linalg.norm(back[:,0]-points[:,0],axis=1)
        delta=nxt[:,0]-points[:,0]
        good=(status[:,0]>0)&(status2[:,0]>0)&(fb<3)&(error[:,0]<30)&(np.linalg.norm(delta,axis=1)<100)
        good&=np.isfinite(delta).all(axis=1)&np.isfinite(fb)
        return delta,good,fb
    delta,good,fb=track(x,4)
    # A coarse pyramid can drag points toward borders/background. Retry failed
    # points locally, retaining exactly the same consistency/appearance gates.
    failed=np.flatnonzero(~good)
    if len(failed):
        retry,reliable,retry_fb=track(x[failed],1)
        accepted=failed[reliable]
        delta[accepted]=retry[reliable];fb[accepted]=retry_fb[reliable];good[accepted]=True
    fallback=np.zeros_like(p) if fallback_velocity is None else np.clip(fallback_velocity,-35,35)
    delta=np.where(good[:,None],delta,fallback)
    return p+delta,good,fb


def bidirectional_prior(gray,points,left,right):
    before=max(0,left-2);after=min(len(points)-1,right+2)
    v0=(points[left]-points[before])/max(left-before,1)
    v1=(points[after]-points[right])/max(after-right,1)
    forward=[points[left].copy()];backward=[points[right].copy()]
    forward_good=[np.ones(101,bool)];backward_good=[np.ones(101,bool)]
    for f in range(left+1,right+1):
        p,g,_=lk_step(gray[f-1],gray[f],forward[-1],v0)
        forward.append(resample(p));forward_good.append(g)
    for f in range(right-1,left-1,-1):
        p,g,_=lk_step(gray[f+1],gray[f],backward[-1],-v1)
        backward.append(resample(p));backward_good.append(g)
    backward=backward[::-1];backward_good=backward_good[::-1]
    t=np.linspace(0,1,right-left+1)[:,None,None];dt=right-left
    hermite=((2*t**3-3*t**2+1)*points[left]+(t**3-2*t**2+t)*v0*dt
             +(-2*t**3+3*t**2)*points[right]+(t**3-t**2)*v1*dt)
    flow=(1-t)*np.array(forward)+t*np.array(backward)
    # LK can attach an occluded tip to the other body arm. A two-sided smooth
    # trajectory remains a separate hypothesis instead of accepting that flow.
    return dict(forward=np.array(forward),backward=np.array(backward),hermite=hermite,
                mean=.5*flow+.5*hermite,disagreement=np.linalg.norm(np.array(forward)-np.array(backward),axis=2))


def select_motion_path(candidates,prior,radius,manual_direction=None):
    """Second-order dynamic programming: position, velocity, and acceleration."""
    # Orient candidates to the already tracked A/B endpoints, not anatomy.
    for i,cs in enumerate(candidates):
        for c in cs:
            c['points']=align(c['points'],prior['mean'][i])
            end_error=np.mean(np.sum((c['points'][[0,-1]]-prior['hermite'][i,[0,-1]])**2,axis=1))/radius**2
            shape_error=np.mean(np.sum((c['points']-prior['mean'][i])**2,axis=1))/radius**2
            c['motion_unary']=c['unary']+.02*shape_error+.05*end_error
    def velocity(i,a,b):
        delta=b-a;expected=prior['mean'][i]-prior['mean'][i-1]
        result=.12*np.mean(np.sum((delta-expected)**2,axis=1))/radius**2
        if manual_direction is not None:
            direction=np.asarray(manual_direction);projection=np.dot(delta[-1],direction)
            result+=1.5*max(0,-projection)**2/radius**2
        return result
    if len(candidates)==1:return [0]
    cost=np.empty((len(candidates[0]),len(candidates[1])))
    for a,ca in enumerate(candidates[0]):
        for b,cb in enumerate(candidates[1]):
            cost[a,b]=ca['motion_unary']+cb['motion_unary']+velocity(1,ca['points'],cb['points'])
    pointers=[]
    for i in range(2,len(candidates)):
        updated=np.full((len(candidates[i-1]),len(candidates[i])),np.inf);ptr=np.zeros(updated.shape,int)
        for b,cb in enumerate(candidates[i-1]):
            for c,cc in enumerate(candidates[i]):
                vals=[]
                for a,ca in enumerate(candidates[i-2]):
                    acceleration=cc['points']-2*cb['points']+ca['points']
                    penalty=.08*np.mean(np.sum(acceleration**2,axis=1))/radius**2
                    penalty+=.12*np.mean(np.sum(acceleration[[0,-1]]**2,axis=1))/radius**2
                    vals.append(cost[a,b]+penalty)
                ptr[b,c]=int(np.argmin(vals));updated[b,c]=min(vals)+cc['motion_unary']+velocity(i,cb['points'],cc['points'])
        pointers.append(ptr);cost=updated
    a,b=np.unravel_index(cost.argmin(),cost.shape);chosen=[int(b),int(a)]
    for ptr in pointers[::-1]:chosen.append(int(ptr[chosen[-1],chosen[-2]]))
    return chosen[::-1]


def gpu_joint_fit(initial,masks,gray,prior,observed,radius,lengths,manual_direction=None):
    """Joint silhouette + measured motion + acceleration fit on CUDA.

    Anchor frames are fixed. Intermediate points stay near their topology
    candidate, so optimization cannot invent an arbitrary new branch order.
    """
    if not torch.cuda.is_available():raise RuntimeError('CUDA required')
    device='cuda';rng=np.random.default_rng(17);samples=[];labels=[]
    for mask in masks:
        inside=np.argwhere(mask);outside=np.argwhere(ndi.binary_dilation(mask,iterations=22)&~mask)
        a=inside[rng.choice(len(inside),1000,replace=len(inside)<1000)]
        b=outside[rng.choice(len(outside),1000,replace=len(outside)<1000)]
        samples.append(np.vstack([a,b])[:,::-1]);labels.append(np.r_[np.ones(1000),np.zeros(1000)])
    samples=torch.tensor(np.array(samples),device=device,dtype=torch.float32)
    labels=torch.tensor(np.array(labels),device=device,dtype=torch.float32)
    anchor_width=[]
    for frame in (0,len(initial)-1):
        field=ndi.distance_transform_edt(masks[frame]);p=initial[frame]
        anchor_width.append(ndi.map_coordinates(field,[p[:,1],p[:,0]],order=1,mode='constant'))
    widths=np.clip(np.mean(anchor_width,axis=0),.5,1.35*radius)
    widths[:2]=[.5,max(1,widths[1])];widths[-2:]=[max(1,widths[-2]),.5]
    widths=torch.tensor(widths,device=device,dtype=torch.float32)
    flow=[];flow_weight=[]
    for i in range(len(initial)-1):
        advanced,good,_=lk_step(gray[i],gray[i+1],initial[i])
        flow.append(advanced-initial[i]);flow_weight.append(good)
    flow=torch.tensor(np.array(flow),device=device,dtype=torch.float32)
    flow_weight=torch.tensor(np.array(flow_weight),device=device,dtype=torch.float32)
    # Occluded tips must not inherit the optical flow of a different body arm.
    for i,obs in enumerate(observed[:-1]):
        if not obs[0]:flow_weight[i,:15]=0
        if not obs[1]:flow_weight[i,-15:]=0
    base=torch.tensor(initial,device=device,dtype=torch.float32)
    limit=np.full(initial.shape[:2],radius*.35,np.float32)
    limit[:,0]=radius*.5;limit[:,-1]=radius*.5;limit[0]=0;limit[-1]=0
    for i,obs in enumerate(observed):
        for j,yes in zip((0,-1),obs):
            if yes:limit[i,j]=0
    limit=torch.tensor(limit,device=device)
    parameter=torch.zeros_like(base,requires_grad=True);opt=torch.optim.Adam([parameter],lr=.07)
    unit=torch.tensor(np.array(lengths)/100,device=device,dtype=torch.float32)[:,None]
    end_prior=torch.tensor(prior['hermite'][:,[0,-1]],device=device,dtype=torch.float32)
    for _ in range(160):
        p=base+torch.tanh(parameter)*limit[:,:,None]
        distances=torch.sqrt(((samples[:,:,None,:]-p[:,None,:,:])**2).sum(-1)+1e-5)
        image=torch.sigmoid((widths[None,None,:]-distances).amax(-1)*.7)
        loss=(image-labels).square().mean()
        delta=p[:,1:]-p[:,:-1];ds=torch.sqrt(delta.square().sum(-1)+1e-5)
        loss+=.04*((ds-unit)/unit).square().mean()
        tangent=delta/ds[:,:,None]
        loss+=.025*(tangent[:,1:]-tangent[:,:-1]).square().sum(-1).mean()
        acceleration=p[2:]-2*p[1:-1]+p[:-2]
        loss+=.04*acceleration.square().sum(-1).mean()/radius**2
        residual=p[1:]-p[:-1]-flow
        loss+=.05*(residual.square().sum(-1)*flow_weight).mean()/radius**2
        loss+=.1*(p-base).square().sum(-1).mean()/radius**2
        loss+=.04*(p[:,[0,-1]]-end_prior).square().sum(-1).mean()/radius**2
        if manual_direction is not None:
            d=torch.tensor(manual_direction,device=device,dtype=torch.float32)
            progress=((p[1:,-1]-p[:-1,-1])*d).sum(-1)
            loss+=.8*torch.relu(-progress).square().mean()/radius**2
        opt.zero_grad();loss.backward();opt.step()
    return np.array([resample(p) for p in (base+torch.tanh(parameter)*limit[:,:,None]).detach().cpu().numpy()])


def intervals(valid,max_gap=18):
    bad=np.flatnonzero(~valid)
    if not len(bad):return []
    groups=np.split(bad,np.where(np.diff(bad)>2)[0]+1);result=[]
    for group in groups:
        left=int(group[0]-1);right=int(group[-1]+1)
        if left>=0 and right<len(valid) and right-left<=max_gap and valid[left] and valid[right]:result.append((left,right))
    return result


def run(sample,worm_filter=None):
    _,source,ids,get_mask,bad=load_sample(sample)
    cap=cv2.VideoCapture(source['video']);gray=[];x0,y0,x1,y1=source['crop_xyxy']
    for _ in range(source['frames']):
        ok,frame=cap.read()
        if not ok:raise RuntimeError('Truncated input video')
        gray.append(cv2.cvtColor(frame[y0:y1,x0:x1],cv2.COLOR_BGR2GRAY))
    cap.release()
    from resource_paths import motion_constraints_path
    constraints=json.loads(motion_constraints_path(ROOT).read_text(encoding='utf-8'))['constraints']
    for worm in ids:
        if worm_filter and worm!=worm_filter:continue
        start_time=time.time();src=ROOT/'outputs'/sample/'wormtracer-corrected'/f'worm-{worm}'
        target=ROOT/'outputs'/sample/'wormtracer-temporal'/f'worm-{worm}';target.mkdir(parents=True,exist_ok=True)
        points=np.load(src/'centerlines.npy');original=points.copy()
        quality=json.loads((src/'quality.json').read_text());meta=json.loads((src/'run.json').read_text())
        ref=meta['reference_length_px'];lengths=np.linalg.norm(np.diff(points,axis=1),axis=2).sum(axis=1)
        reliable=np.array([q['centerline_valid'] for q in quality])&(lengths>.94*ref)&(lengths<1.08*ref)
        windows=intervals(reliable);records=[]
        print(sample,worm,'motion windows',windows,flush=True)
        for window_index,(left,right) in enumerate(windows):
            report_progress(window_index,len(windows),f'分析前后帧运动：{left}–{right}',unit='个区间',force=True)
            if any(bad.get((f,worm),False) for f in range(left,right+1)):continue
            prior=bidirectional_prior(gray,original,left,right)
            mask_list=[get_mask(worm,f) for f in range(left,right+1)]
            length_prior=np.linspace(lengths[left],lengths[right],right-left+1)
            all_candidates=[];infos=[];radius_list=[]
            manual=next((c for c in constraints if c['sample']==sample and c['worm_id']==worm
                         and left<c['frames'][1] and right>c['frames'][0]),None)
            direction=manual['direction_xy'] if manual else None
            for i,f in enumerate(range(left,right+1)):
                mask=mask_list[i];c,zone,info=topology_candidates(mask,prune=True)
                radius_list.append(info['radius_px']);infos.append(info)
                for v in c:v['endpoint_observed']=[True,True]
                c+=hidden_tip_candidates(mask,length_prior[i])
                if f in (left,right):c=[dict(points=original[f],name='reliable_anchor',connection_cost=0.,endpoint_observed=[True,True])]
                else:
                    for name in ('hermite','forward','backward'):
                        c.append(dict(points=resample(prior[name][i]),name='motion_'+name,connection_cost=0.,endpoint_observed=[False,False]))
                field,origin=local_field(mask)
                for v in c:
                    p=v['points'];reverse=np.mean((p[::-1]-prior['mean'][i])**2)<np.mean((p-prior['mean'][i])**2)
                    if reverse:v['points']=p[::-1].copy();v['endpoint_observed']=v['endpoint_observed'][::-1]
                    candidate_score(v,mask,field,origin,length_prior[i],info['radius_px'],None)
                # A temporally smooth prediction outside the observed body is
                # not evidence. Retain it only when its image support is sound.
                supported=[v for v in c if not v['name'].startswith('motion_') or
                           (v['inside_fraction']>=.97 and v['reconstruction_iou']>=.55 and not v['local_hairpin'])]
                if supported:c=supported
                all_candidates.append(c)
            radius=float(np.median(radius_list));chosen=select_motion_path(all_candidates,prior,radius,direction)
            selected=[c[k] for c,k in zip(all_candidates,chosen)]
            fitted=gpu_joint_fit(np.array([c['points'] for c in selected]),mask_list,gray[left:right+1],prior,
                                  [c['endpoint_observed'] for c in selected],radius,length_prior,direction)
            for i,f in enumerate(range(left+1,right),1):
                p=fitted[i];qa=geometry_quality(p);mask=mask_list[i];c=selected[i]
                point_inside=ndi.map_coordinates(mask.astype(np.uint8),[p[:,1],p[:,0]],order=0,mode='constant')>0
                inside=float(point_inside.mean())
                reasons=[];ranges=[]
                for endpoint,observed in enumerate(c['endpoint_observed']):
                    if not observed:ranges.append([0,15] if endpoint==0 else [85,100])
                if ranges:reasons.append('endpoint_inferred_from_motion')
                if qa['local_hairpin']:reasons.append('local_hairpin')
                if inside<.97:reasons.append('centerline_outside_mask')
                outside=np.flatnonzero(~point_inside)
                if len(outside):
                    for group in np.split(outside,np.where(np.diff(outside)>1)[0]+1):
                        ranges.append([max(0,int(group[0])-3),min(100,int(group[-1])+3)])
                length=float(np.linalg.norm(np.diff(p,axis=0),axis=1).sum())
                if not .85*ref<length<1.15*ref:reasons.append('length_outlier')
                if 'length_outlier' in reasons:ranges=[[0,100]]
                # Do not accept a motion hypothesis that introduces a tight
                # spurious loop or substantially leaves the observed silhouette.
                if qa['local_hairpin'] or inside<.9:
                    quality[f]['temporal_attempt']='rejected';continue
                points[f]=p
                quality[f].update(selected_candidate=c['name'],topology='temporal_contact' if ranges else infos[i]['kind'],
                                  centerline_valid=not reasons,quality_reasons=reasons,inside_fraction=inside,
                                  uncertain_point_ranges=ranges,endpoint_a_inferred=not c['endpoint_observed'][0],
                                  endpoint_b_inferred=not c['endpoint_observed'][1],temporal_anchors=[left,right],
                                  temporal_bidirectional_disagreement_px=float(np.median(prior['disagreement'][i])),
                                  manual_direction_applied=manual is not None,**qa)
            records.append(dict(anchors=[left,right],manual_constraint=manual,
                                selected=[c['name'] for c in selected],
                                forward_backward_median_px=np.median(prior['disagreement'],axis=1).tolist()))
            print('completed',left,right,[c['name'] for c in selected[1:-1]],flush=True)
        report_progress(len(windows),len(windows),'时序区间检查完成，保存结果',unit='个区间',fraction=.99,force=True)
        np.save(target/'centerlines.npy',points)
        (target/'quality.json').write_text(json.dumps(quality,indent=2),encoding='utf-8')
        (target/'motion-windows.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
        meta.update(algorithm='Bidirectional image optical flow + hidden-tip graph candidates + second-order motion selection + joint CUDA silhouette fit',
                    source=str(src),seconds=time.time()-start_time,motion_windows=len(records),
                    valid_frames=sum(q['centerline_valid'] for q in quality),device=torch.cuda.get_device_name())
        (target/'run.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
        print('saved',sample,worm,'seconds',round(time.time()-start_time,1),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--sample',default='2-1-1314');p.add_argument('--worm',type=int)
    args=p.parse_args()
    for sample in (['2-1-1314','5-1-910'] if args.sample=='all' else [args.sample]):run(sample,args.worm)
