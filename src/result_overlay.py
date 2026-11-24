"""Contour-first review rendering; rejected poses stay visibly uncertain."""
import cv2
import numpy as np

from frame_metrics import present_frame
from quality_schema import flag
from functools import lru_cache
import json
from PIL import Image,ImageDraw,ImageFont
from framewise_pose import arc_midpoint

CONTOUR = (55, 220, 55)
OVERLAY_VERSION = 'endpoint-evidence-v4-darker-tail-tip'
CENTERLINE = (250, 250, 250)
UNCERTAIN = (0, 180, 255)


def draw_endpoint(image, xy, label):
    color = (255,200,0) if label.startswith('H') else (0,110,255) if label.startswith('T') else (215,215,215)
    thickness = -1 if label in ('H','T') else 2
    cv2.circle(image,xy,5,(45,45,45),3,cv2.LINE_AA)
    cv2.circle(image,xy,5,color,thickness,cv2.LINE_AA)
    (w,h),baseline=cv2.getTextSize(label,0,.62,1)
    left=max(2,min(xy[0]+8,image.shape[1]-w-2))
    bottom=xy[1]-12-baseline
    if bottom<h+2:
        bottom=min(image.shape[0]-baseline-1,xy[1]+12+h)
    pos=(left,bottom)
    cv2.rectangle(image,(pos[0]-2,pos[1]-h-2),(pos[0]+w+1,pos[1]+baseline),(40,40,40),-1)
    cv2.putText(image,label,pos,0,.62,color,1,cv2.LINE_AA)


def draw_pose_endpoint(image, xy, label, view):
    side='head' if label.startswith('H') else 'tail' if label.startswith('T') else None
    darker=view.get('blur_darker_tips',{}).get(side)
    estimate=darker if darker is not None else view.get('blur_midpoints',{}).get(side)
    if estimate is not None:
        xy=np.rint(estimate).astype(int)
        label+='~'
        if darker is not None:
            cv2.circle(image,tuple(xy),8,(225,110,20),2,cv2.LINE_AA)
    draw_endpoint(image,tuple(xy),label)


@lru_cache(maxsize=2)
def estimated_end_label(symbol):
    text = symbol+'（估计）'
    for name in ('msyh.ttc', 'PingFang.ttc', 'NotoSansCJK-Regular.ttc'):
        try:
            font = ImageFont.truetype(name, 16)
            break
        except OSError:
            continue
    else:
        font = ImageFont.load_default()
        text = symbol+' (est.)'
    left, top, right, bottom = font.getbbox(text)
    label = Image.new('RGB', (right-left+6, bottom-top+6), '#17313d')
    ImageDraw.Draw(label).text((3-left, 3-top), text, font=font, fill='#9cedff' if symbol=='H' else '#ffba78')
    return cv2.cvtColor(np.asarray(label), cv2.COLOR_RGB2BGR)


def draw_estimate(image, mask, xy, symbol, occupied):
    x,y=xy
    cv2.circle(image,(x,y),6,(255,210,85) if symbol=='H' else (0,140,255),2,cv2.LINE_AA)
    label=estimated_end_label(symbol)
    h,w=label.shape[:2]
    if h>image.shape[0] or w>image.shape[1]:
        return
    boxes=[(int(np.clip(dx,0,image.shape[1]-w)),int(np.clip(dy,0,image.shape[0]-h)))
           for dy in (y-h,y+4) for dx in (x+8,x-8-w)]
    def cost(box):
        a,b=box
        overlap=sum(max(0,min(a+w,r)-max(a,l))*max(0,min(b+h,t)-max(b,s)) for l,s,r,t in occupied)
        return overlap,np.count_nonzero(mask[b:b+h,a:a+w])
    left,top=min(boxes,key=cost)
    image[top:top+h,left:left+w]=label
    occupied.append((left,top,left+w,top+h))


def draw_pose(image, mask, points, row, view):
    estimated=set()
    if points is not None:
        mid = arc_midpoint(points)
        points = np.rint(points).astype(np.int32)
        if view['posture_valid'] and row.get('display_source','formal')=='formal':
            cv2.polylines(image, [points], False, CENTERLINE, 1, cv2.LINE_AA)
            if mid is not None:
                x, y = np.rint(mid).astype(int)
                if 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and mask[y,x]:
                    cv2.circle(image, (x,y), 4, (200,55,218), -1, cv2.LINE_AA)
        else:
            for k in range(len(points)-1):
                if k % 8 < 4:
                    cv2.line(image, tuple(points[k]), tuple(points[k+1]), UNCERTAIN, 2, cv2.LINE_AA)
        if row.get('body_sequence_version'):
            from head_recovery import tracking_indices
            from centerline_topology import resample
            for index,xy in zip(tracking_indices(),resample(points)[tracking_indices()]):
                color=(255,210,85) if index<50 else (0,145,255) if index>50 else (240,240,240)
                if view['endpoint_labels'][0]=='A':
                    color=(215,215,215)
                cv2.circle(image,tuple(np.rint(xy).astype(int)),2,color,1,cv2.LINE_AA)
    if points is not None and view['head_section_status']=='body_sequence_estimate':
        occupied=[]
        for symbol,field in [('H','head_display_estimated_end'),('T','tail_display_estimated_end')]:
            end=view.get(field)
            if end in (0,1):
                draw_estimate(image,mask,points[0 if end==0 else -1],symbol,occupied)
                estimated.add(0 if end==0 else -1)
    for xy in json.loads(row.get('head_visible_xy') or '[]'):
        cv2.circle(image, tuple(np.rint(xy).astype(int)), 2, (180,245,255), 1, cv2.LINE_AA)
    front = np.rint(json.loads(row.get('head_forebody_xy') or '[]')).astype(int)
    for xy in front:
        cv2.circle(image, tuple(xy), 4, (0,145,255), 2, cv2.LINE_AA)
    rear=np.rint(json.loads(row.get('tail_hindbody_xy') or '[]')).astype(int)
    for xy in rear:
        cv2.circle(image,tuple(xy),4,(96,196,72),2,cv2.LINE_AA)
    if view['head_section_status'] in ('forebody_only','hindbody_only','trajectory_estimate'):
        occupied=[]
        for symbol,field in [('H','head_display_estimated_end'),('T','tail_display_estimated_end')]:
            end=view.get(field)
            if points is not None and end in (0,1):
                draw_estimate(image,mask,points[0 if end==0 else -1],symbol,occupied)
        if not occupied and len(front) and view['head_section_status']=='forebody_only':
            cv2.putText(image,'H-side',tuple(front[0]+[5,-6]),0,.5,(255,200,0),1,cv2.LINE_AA)
        if not occupied and len(rear) and view['head_section_status']=='hindbody_only':
            cv2.putText(image,'T-side',tuple(rear[0]+[5,-6]),0,.5,(0,140,255),1,cv2.LINE_AA)
        return
    if points is None: return
    for index, label in zip((0,-1),view['endpoint_labels']):
        if index in estimated:continue
        draw_pose_endpoint(image,points[index],label,view)



def draw_worm(image, mask, points, row, *, label=None):
    view=present_frame(row)
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(image, contours, -1, CONTOUR, 2, cv2.LINE_AA)
    yy,xx=np.nonzero(mask)
    if len(xx):
        xy=(int(xx.mean()),int(yy.mean()))
        label=(label or f'W{row["worm_id"]}')+' '+('LEGACY' if view['quality']['legacy'] else 'ID REVIEW' if not view['identity_valid'] else 'REVIEW' if not view['posture_valid'] else '')
        cv2.putText(image,label,(xy[0]+7,xy[1]+17),0,.5,(0,0,0),3,cv2.LINE_AA)
        cv2.putText(image,label,(xy[0]+7,xy[1]+17),0,.5,CONTOUR if view['posture_valid'] else UNCERTAIN,1,cv2.LINE_AA)
    if 'display_tips' in view:
        if points is not None:
            cv2.polylines(image, [np.rint(points).astype(np.int32)], False, CENTERLINE, 1, cv2.LINE_AA)
        for side, point in view['display_tips'].items():
            xy = tuple(np.rint(point).astype(int))
            draw_endpoint(image,xy,'H?' if side=='head' else 'T?')
        return
    if points is None: return
    points = np.rint(points).astype(np.int32)
    valid = view['posture_valid'] and row.get('display_source','formal')=='formal'
    cv2.polylines(image, [points], False, CENTERLINE, 1, cv2.LINE_AA)
    if 'self_contact_state' in row:
        point_color = (43, 213, 255)
        for point in points:
            cv2.circle(image, tuple(point), 1, point_color, -1, cv2.LINE_AA)
        for index, label, endpoint in zip((0,-1), view['endpoint_labels'], ('a','b')):
            color = ((42,76,246) if label.startswith('H') else (225,161,0)
                     if label.startswith('T') else (210,210,210))
            xy = tuple(points[index])
            filled = valid and not flag(row, f'endpoint_{endpoint}_inferred')
            cv2.circle(image, xy, 4, color, -1 if filled else 1, cv2.LINE_AA)
            cv2.putText(image, label, (xy[0]+6,xy[1]-6), 0, .45, color, 1, cv2.LINE_AA)
        return
    labels = view['endpoint_labels']
    if 'endpoint_order_block' in row and row['endpoint_order_block'] in ('', None):
        for point in points[[0,-1]]:
            cv2.circle(image, tuple(point), 4, (170,170,170), -1, cv2.LINE_AA)
        return
    for index,label in zip((0,-1),labels):
        draw_pose_endpoint(image,points[index],label,view)
