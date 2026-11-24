"""Check head and neck chord directions against local silhouette cross-sections."""
import numpy as np
from scipy import ndimage


def turn(head, neck):
    return float(np.degrees(np.arctan2(neck[0]*head[1]-neck[1]*head[0], neck@head)))


def angle_difference(a, b):
    return (a-b+180)%360-180


def cross_section_centers(mask, points, normals, reach):
    offsets=np.arange(-2*reach,2*reach+1)*.5
    xy=points[:,None,:]+offsets[None,:,None]*normals[:,None,:]
    low=np.floor(xy.min(axis=(0,1))).astype(int)-1
    high=np.ceil(xy.max(axis=(0,1))).astype(int)+2
    if np.any(low<0) or np.any(high>mask.shape[::-1]):return None
    patch=mask[low[1]:high[1],low[0]:high[0]].astype(np.float32)
    samples=ndimage.map_coordinates(patch,(xy-low).transpose(2,0,1)[::-1],order=1)
    center=2*reach
    if np.any(samples[:,center]<=.5):return None
    crossings=[]
    for side in (samples[:,center::-1],samples[:,center:]):
        outside=side<=.5
        if not outside.any(axis=1).all():return None
        edge=np.argmax(outside,axis=1)
        index=np.arange(len(points))
        a,b=side[index,edge-1],side[index,edge]
        crossings.append(.5*(edge-1+(a-.5)/(a-b)))
    shift=(crossings[1]-crossings[0])/2
    if np.max(np.abs(shift))>2:return None
    return points+shift[:,None]*normals


def section_direction(points, mask):
    """Fit the inner 60% of a segment only if its full path is locally straight."""
    length=np.linalg.norm(points[-1]-points[0])
    if length<8:return None
    pad=round(.2*(len(points)-1))
    inner=points[pad:-pad]
    stations=np.arange(len(inner))-(len(inner)-1)/2
    axis=stations@(inner-inner.mean(axis=0))
    span=np.linalg.norm(axis)
    if span<1:return None
    axis/=span
    normal=np.array([-axis[1],axis[0]])
    # The original chord remains the definition on genuinely curved sections.
    if np.abs((points-inner.mean(axis=0))@normal).max()>2:return None
    centers=cross_section_centers(mask,inner,np.broadcast_to(normal,inner.shape),int(np.ceil(.7*length)))
    if centers is None:return None
    observed=stations@(centers-centers.mean(axis=0))
    observed/=np.linalg.norm(observed)
    residual=(centers-centers.mean(axis=0))@np.array([-observed[1],observed[0]])
    if np.abs(residual).max()>1:return None
    return axis,observed,length


def local_angle(points, mask):
    """Measure the segment axes, one-pixel angular resolution, and landmark chord."""
    head=section_direction(points[:11],mask)
    if head is None:return None
    neck=section_direction(points[10:31],mask)
    if neck is None:return None
    resolution=np.degrees(np.arctan(1/head[2])+np.arctan(1/neck[2]))
    stations=np.array([10,30])
    tangents=points[stations+2]-points[stations-2]
    spans=np.linalg.norm(tangents,axis=1)
    if np.min(spans)<1:return None
    normals=np.column_stack((-tangents[:,1],tangents[:,0]))/spans[:,None]
    landmarks=cross_section_centers(mask,points[stations],normals,int(np.ceil(.7*head[2])))
    if landmarks is None:return None
    landmark_angle=turn(points[0]-landmarks[0],landmarks[0]-landmarks[1])
    return turn(head[0],neck[0]),turn(head[1],neck[1]),resolution,landmark_angle


def supported_angles(raw, evidence, segments, times):
    """Only use a correction supported in time on both sides of the measured frame."""
    fitted,outline,resolution,landmark=evidence.T
    delta=angle_difference(fitted,raw)
    edge_delta=angle_difference(outline,raw)
    landmark_delta=angle_difference(landmark,raw)
    disagreement=np.abs(angle_difference(outline,fitted))
    agrees=disagreement<=resolution
    result=raw.copy()
    method=np.where(np.isfinite(raw),'centerline_chords','unavailable').astype(object)
    linked=(segments[1:-1]==segments[:-2]) & (segments[1:-1]==segments[2:])
    conflict=np.isfinite(raw) & (disagreement>2*resolution)
    centers=np.flatnonzero(linked & conflict[:-2] & conflict[1:-1] & conflict[2:])+1
    for frames in (centers-1,centers,centers+1):
        result[frames]=np.nan
        method[frames]='review_geometry'
    supported=agrees & np.isfinite(raw)
    corrections=linked & supported[:-2] & supported[1:-1] & supported[2:]
    for i in np.flatnonzero(corrections)+1:
        if (abs(delta[i])<=.5*resolution[i] or delta[i]*edge_delta[i]<=0
                or landmark_delta[i]*edge_delta[i]<=0):continue
        neighbor=edge_delta[[i-1,i+1]]
        expected=np.interp(times[i],times[[i-1,i+1]],neighbor)
        if abs(edge_delta[i]-expected)>resolution[i] or not np.any(neighbor*edge_delta[i]>0):continue
        # Use the measured angle at i. Adjacent frames verify the correction
        # without averaging the motion peak into its neighbors.
        # Respect the chord at the original 10% and 30% stations, including
        # half a pixel of edge quantization. The interior slope alone can
        # overstate the bend of a smoothly curved neck.
        change=min(abs(edge_delta[i]),abs(landmark_delta[i])+.5*resolution[i])
        result[i]=angle_difference(raw[i]+np.copysign(change,edge_delta[i]),0)
        method[i]='section_fit'
    return result,method
