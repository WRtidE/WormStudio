"""Initialize once from user anchors or terminal taper, then preserve endpoint order."""
import numpy as np
from head_temporal import geometry_link,MAX_INTERVAL_S


def initialize(results,poses,segments,flipped,identities,profiles,anchors,cfg,fps,mode):
    from head_tail import taper_votes,vote_winner
    if not np.isfinite(fps) or fps<=0:raise ValueError('初始化需要有效帧率')
    for r in results:
        r.update(head_local_endpoint=None,head_local_reason=r['head_reason'],head_temporal_block=-1,
                 head_temporal_seed_frame=None,head_temporal_conflict=False,head_temporal_changed=False,
                 head_temporal_link_rms_bl=None,head_temporal_gap_s=None)
    usable={i for g in segments for i in g}
    bad_identity={identities[a['frame']] for a in anchors if a['frame'] not in usable}
    def ordered(i):return poses[i,::-1] if flipped[i] else poses[i]
    blocks=[]
    for sid,g in enumerate(segments):
        for i in g:results[i].update(head_segment=sid,head_input_flipped=bool(flipped[i]))
        if blocks:
            old,parity,_=blocks[-1][-1];i,j=old[-1],g[0]
            if (j-i)/fps<=MAX_INTERVAL_S and all(identities[k]==identities[i] for k in range(i,j+1)):
                match=geometry_link(ordered(i),ordered(j))
                if match is not None:
                    match['gap_s']=(j-i)/fps;blocks[-1].append((g,parity^match['flip'],match));continue
        blocks.append([(g,0,None)])
    for bid,block in enumerate(blocks):
        frame_parity={i:parity for g,parity,_ in block for i in g};ids=list(frame_parity)
        local=[a for a in anchors if a['frame'] in frame_parity]
        reason='automatic_detection_disabled';head=None;score=0.;seed=None;votes=[];invalid=False;conflict=False
        if local:
            av=[]
            for a in local:
                i=a['frame'];line=ordered(i);dist=np.linalg.norm(line[[0,-1]]-a['point'],axis=1)
                length=np.linalg.norm(np.diff(line,axis=0),axis=1).sum()
                # A central point equidistant from both ends cannot establish identity.
                if dist.min()>cfg['manual_max_distance_bl']*length or abs(dist[0]-dist[1])<cfg['min_assignment_margin_bl']*length:
                    invalid=True
                else:av.append(int(np.argmin(dist))^frame_parity[i])
            conflict=len(set(av))>1
            if invalid:reason='manual_point_not_near_endpoint'
            elif conflict:reason='manual_anchor_conflict'
            else:head=av[0];score=1.;seed=min(a['frame'] for a in local);reason='manual_anchor'
        elif identities[ids[0]] in bad_identity:
            reason='manual_anchor_unreliable';invalid=True
        elif mode=='taper_seed':
            start=ids[0];initial=[i for i in ids if (i-start)/fps<=.5][:5]
            seed_votes=[]
            for i in initial:
                vote=taper_votes(profiles,[i],flipped,cfg)
                if vote:seed_votes.append((i,vote[0][0]^frame_parity[i],vote[0][1]))
            votes=[(end,value) for _,end,value in seed_votes]
            if len(votes)<3:reason='initial_taper_insufficient'
            else:
                head,score=vote_winner(votes,cfg['min_vote_fraction'])
                reason='initial_taper_conflict' if head is None else 'taper_seed_initialization'
                if head is not None:seed=seed_votes[0][0]
        for g,parity,match in block:
            ga=[a for a in local if a['frame'] in g]
            for i in g:
                r=results[i];r.update(head_anchor_count=len(ga),head_anchor_conflict=conflict,
                    head_shape_votes=len(votes),head_reason=reason,head_evidence_score=score,
                    head_temporal_block=bid,head_temporal_seed_frame=seed,
                    head_temporal_link_rms_bl=None if match is None else match['rms_bl'],
                    head_temporal_gap_s=None if match is None else match['gap_s'])
                if head is None:continue
                confirmed=bool(ga)
                r.update(head_endpoint=head^parity^int(flipped[i]),head_identity_confirmed=confirmed,
                    head_status='confirmed_by_user_point' if confirmed else 'unconfirmed_temporal_candidate' if local else 'unconfirmed_taper_candidate',
                    head_reason='manual_anchor' if confirmed else 'manual_seed_propagated' if local else reason)
    for a in anchors:
        if a['frame'] not in usable:results[a['frame']].update(head_reason='manual_anchor_unreliable',head_anchor_count=1)
    return results
