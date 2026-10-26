"""Contour-first review rendering; rejected poses stay visibly uncertain."""
import cv2
import numpy as np

from frame_metrics import present_frame

CONTOUR = (55, 220, 55)
CENTERLINE = (250, 250, 250)
UNCERTAIN = (0, 180, 255)


def draw_worm(image, mask, points, row):
    view=present_frame(row)
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(image, contours, -1, CONTOUR, 2, cv2.LINE_AA)
    yy,xx=np.nonzero(mask)
    if len(xx):
        xy=(int(xx.mean()),int(yy.mean()))
        label=f'W{row["worm_id"]} '+('LEGACY' if view['quality']['legacy'] else 'ID REVIEW' if not view['identity_valid'] else 'REVIEW' if not view['posture_valid'] else '')
        cv2.putText(image,label,(xy[0]+7,xy[1]+17),0,.5,(0,0,0),3,cv2.LINE_AA)
        cv2.putText(image,label,(xy[0]+7,xy[1]+17),0,.5,CONTOUR if view['posture_valid'] else UNCERTAIN,1,cv2.LINE_AA)
    if points is None: return
    points = np.rint(points).astype(np.int32)
    valid = view['posture_valid'] and row.get('display_source','formal')=='formal'
    if valid:
        cv2.polylines(image, [points], False, CENTERLINE, 1, cv2.LINE_AA)
    else:
        for k in range(len(points)-1):
            if k % 8 < 4:
                cv2.line(image, tuple(points[k]), tuple(points[k+1]), UNCERTAIN, 2, cv2.LINE_AA)
    # Display names are fixed; confirmation is reported separately in the HUD/UI.
    labels = ('H','T')
    for index, label, color in [(0, labels[0], (255,200,0)), (-1, labels[1], (0,110,255))]:
        # Endpoint identity is distinguished by color even before head confirmation.
        # Amber dashes describe pose quality, not head confirmation.
        xy = tuple(points[index])
        cv2.circle(image, xy, 4, color, -1, cv2.LINE_AA)
        cv2.putText(image, label, (xy[0]+6,xy[1]-6), 0, .45, color, 1, cv2.LINE_AA)


def draw_hud(image, rows, frame, fps):
    # Image remains readable even for the small cropped videos used in tests.
    width = image.shape[1]
    scale = min(.55, max(.28, width/1750))
    step = max(16, int(32*scale/.55))
    lines = [f'Frame {frame} | {frame/fps:.3f}s | green: contour | white: measured | amber dashed: review pose']
    def fmt(value, digits=1):
        return '--' if value is None else f'{value:.{digits}f}'
    for row in rows:
        m = present_frame(row)
        pixel=m.get('measurement_mode')=='pixels'
        unit='px' if pixel else 'um'
        def measurement(key): return m.get(key.replace('_um','_px') if pixel else key)
        identity='LEGACY' if m['quality']['legacy'] else 'ID CONTINUOUS' if m['identity_valid'] else 'ID REVIEW / anonymous area'
        lines += [f'W{m["worm_id"]} {identity}  L={fmt(measurement("length_um"))} {unit}   Area={fmt(measurement("area_um2"),0)} {unit}^2   v={fmt(measurement("speed_um_s"))} {unit}/s',
                  f'  Vol(est)={fmt(measurement("volume_um3"),0)} {unit}^3   Head angle={fmt(m["head_angle_deg"])} deg   '+('head confirmed' if m['head_confirmed'] else 'head unconfirmed')]
    bottom = min(image.shape[0], len(lines)*step+10)
    cv2.rectangle(image, (0,0), (width,bottom), (24,29,28), -1)
    for j, text in enumerate(lines):
        cv2.putText(image,text,(8,step*(j+1)),0,scale,(240,245,240),1,cv2.LINE_AA)
