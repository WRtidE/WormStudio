"""Sample real grayscale inside the worm, without assuming which end is the head."""
import cv2
import numpy as np
from scipy import ndimage


def sample_profile(gray, points, radii):
    tangent=np.gradient(points,axis=0)
    tangent/=np.maximum(np.linalg.norm(tangent,axis=1,keepdims=True),1e-9)
    normal=np.column_stack([-tangent[:,1],tangent[:,0]])
    offsets=np.linspace(-.35,.35,5)
    q=points[:,None,:]+normal[:,None,:]*np.maximum(radii,0)[:,None,None]*offsets[None,:,None]
    values=ndimage.map_coordinates(gray.astype(float),[q[:,:,1].ravel(),q[:,:,0].ravel()],order=1,mode='nearest').reshape(len(points),5)
    profile=np.median(values,axis=1)
    profile=ndimage.gaussian_filter1d(profile,1.)
    scale=float(np.std(profile))
    if scale<3 or np.median(radii)<1:return np.full(len(points),np.nan)
    return (profile-np.mean(profile))/scale


def extract_profiles(source, poses, radii, usable):
    out=np.full(poses.shape[:2],np.nan)
    cap=cv2.VideoCapture(source['video']);x0,y0,x1,y1=source['crop_xyxy']
    try:
        for i in range(len(poses)):
            ok,frame=cap.read()
            if not ok:raise ValueError('Cannot read source frame for head appearance')
            if usable[i] and np.isfinite(poses[i]).all() and np.isfinite(radii[i]).all():
                gray=cv2.cvtColor(frame[y0:y1,x0:x1],cv2.COLOR_BGR2GRAY)
                out[i]=sample_profile(gray,poses[i],radii[i])
    finally:cap.release()
    return out
