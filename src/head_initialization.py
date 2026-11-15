"""Initialize once from user anchors or terminal taper, then preserve endpoint order."""
import numpy as np
from head_temporal import geometry_link, visible_tip_link, MAX_INTERVAL_S


def initial_taper_seed(block, profiles, flipped, cfg, fps, visible):
    """Use the first supported local initialization in the common physical point order."""
    from head_tail import taper_votes, vote_winner
    windows = []
    for frames, parity, match in block:
        # A newly joined clip may supply a seed if the preceding clip had none.
        # Keep its original initialization window; don't combine sparse votes
        # from different turns or let a later taper vote flip an established head.
        if not windows or (match and match.get('source') == 'centerline_overlap'):
            windows.append([])
            start = None
        window = windows[-1]
        for i in frames:
            if len(window) == 5 or (start is not None and (i - start) / fps > .5):
                break
            if visible is not None and not visible[i].all():
                continue
            if start is None:
                start = i
            window.append((i, parity))
    failure = (None, 0., None, [], 'initial_taper_insufficient')
    for window in windows:
        votes = []
        first = None
        for i, parity in window:
            local = taper_votes(profiles, [i], flipped, cfg)
            if local:
                if first is None:
                    first = i
                end, weight = local[0]
                votes.append((end ^ parity, weight))
        if len(votes) >= 3:
            head, score = vote_winner(votes, cfg['min_vote_fraction'])
            if head is not None:
                return head, score, first, votes, 'taper_seed_initialization'
            failure = (None, score, None, votes, 'initial_taper_conflict')
        elif len(votes) > len(failure[3]):
            failure = (None, 0., None, votes, 'initial_taper_insufficient')
    return failure


def initialize(results,poses,segments,flipped,identities,profiles,anchors,cfg,fps,mode,endpoint_visible=None,valid=None,links=None,pose_gap_allowed=None):
    if not np.isfinite(fps) or fps<=0:raise ValueError('初始化需要有效帧率')
    for r in results:
        r.update(head_local_endpoint=None,head_local_reason=r['head_reason'],head_temporal_block=-1,
                 head_temporal_seed_frame=None,head_temporal_conflict=False,head_temporal_changed=False,
                 head_temporal_link_rms_bl=None,head_temporal_gap_s=None,head_temporal_link_source=None)
    usable={i for g in segments for i in g}
    bad_identity={identities[a['frame']] for a in anchors if a['frame'] not in usable}
    def ordered(i):return poses[i,::-1] if flipped[i] else poses[i]
    blocks=[]
    for sid,g in enumerate(segments):
        for i in g:results[i].update(head_segment=sid,head_input_flipped=bool(flipped[i]))
        if blocks:
            old,parity,_=blocks[-1][-1];i,j=old[-1],g[0]
            permitted = pose_gap_allowed is None or (valid[i+1:j] | pose_gap_allowed[i+1:j]).all()
            if permitted and (j-i)/fps<=MAX_INTERVAL_S and all(identities[k]==identities[i] for k in range(i,j+1)):
                match=geometry_link(ordered(i),ordered(j))
                if match is None and endpoint_visible is not None and j == i + 1:
                    vi = endpoint_visible[i, ::-1] if flipped[i] else endpoint_visible[i]
                    vj = endpoint_visible[j, ::-1] if flipped[j] else endpoint_visible[j]
                    match = visible_tip_link(ordered(i), ordered(j), vi, vj, cfg)
                curve_match = (links or {}).get((i, j))
                if curve_match is not None:
                    order = int(curve_match['flip'] ^ flipped[i] ^ flipped[j])
                    if match is None or match['flip'] != order:
                        # A verified body correspondence joins the clips before
                        # either can independently choose a head from appearance.
                        match = dict(curve_match, flip=order, source='centerline_overlap')
                if match is not None:
                    match['gap_s']=(j-i)/fps;blocks[-1].append((g,parity^match['flip'],match));continue
        blocks.append([(g,0,None)])
    native_flip = flipped.copy()
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
            head, score, seed, votes, reason = initial_taper_seed(block, profiles, flipped, cfg, fps, endpoint_visible)
        for g,parity,match in block:
            native_flip[g] ^= bool(parity)
            ga=[a for a in local if a['frame'] in g]
            for i in g:
                r=results[i];r.update(head_anchor_count=len(ga),head_anchor_conflict=conflict,
                    head_shape_votes=len(votes),head_reason=reason,head_evidence_score=score,
                    head_temporal_block=bid,head_temporal_seed_frame=seed,
                    head_temporal_link_rms_bl=None if match is None else match['rms_bl'],
                    head_temporal_gap_s=None if match is None else match['gap_s'],
                    head_temporal_link_source=None if match is None else match.get('source','whole_body'))
                if match is not None and match.get('source') == 'centerline_overlap':
                    r['head_trajectory_frames'] = match['frames']
                if head is None:continue
                confirmed=bool(ga)
                r.update(head_endpoint=head^parity^int(flipped[i]),head_identity_confirmed=confirmed,
                    head_status='confirmed_by_user_point' if confirmed else 'unconfirmed_temporal_candidate' if local else 'unconfirmed_taper_candidate',
                    head_reason='manual_anchor' if confirmed else 'manual_seed_propagated' if local else reason)
    for a in anchors:
        if a['frame'] not in usable:results[a['frame']].update(head_reason='manual_anchor_unreliable',head_anchor_count=1)
    if endpoint_visible is not None and valid is not None and len(poses):
        from head_trajectory import fill_from_trajectories
        fill_from_trajectories(results, poses, valid, endpoint_visible, identities, native_flip,
                               fps, cfg, {a['frame'] for a in anchors}, links or {})
    return results
