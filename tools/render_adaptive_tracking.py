"""Render the single-worm size-adaptive review from a completed TAPNext++ run."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

import cv2
import imageio_ffmpeg
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from adaptive_endpoints import review_output
from endpoint_order import sampled
from analyze_phenotypes import ROOT, fitted_mask_source, load_sample
from display_centerlines import fill_display_gaps
from quality_stage import load_guards


PANEL_W, PANEL_H = 704, 398
BAR_HEIGHT, FOOT_HEIGHT = 36, 48
BODY_COLOR = (30, 220, 255)
DIRECTION_COLOR = {'forward': (240,195,35), 'backward': (204,105,240)}


def load_tracks(sample, worm, source):
    from track_order_evidence import read_tracks
    return read_tracks(ROOT/'outputs'/sample/'centerlines-tapnext'/f'worm-{worm}', source)


def camera(positions, length):
    low, high = positions.min(axis=0), positions.max(axis=0)
    span = np.maximum(high-low + .13*length, [.70*length,.44*length])
    factor = min((PANEL_W-24)/span[0], (PANEL_H-24)/span[1])
    return factor, np.array([PANEL_W,PANEL_H])/2 - (low+high)*factor/2


def view(frame, origin, factor, shift):
    matrix = np.column_stack((np.eye(2)*factor, shift-factor*origin))
    return cv2.warpAffine(frame,matrix,(PANEL_W,PANEL_H),borderMode=cv2.BORDER_CONSTANT,
                          borderValue=(46,46,46))


def pixels(points, factor, shift):
    return np.rint(points*factor+shift).astype(np.int32)


def row_points(writer, frame, group, window, anchor, indexes, positions, visible, origin):
    for idx, pt, vis in zip(indexes,positions+origin,visible):
        writer.writerow([frame,group,window,anchor,int(idx),round(float(pt[0]),4),
                         round(float(pt[1]),4),int(vis) if vis is not None else ''])


def render(folder, source, rows, display, records, summary, get_mask):
    from PIL import Image, ImageDraw, ImageFont
    font_path = next((p for p in (Path('/System/Library/Fonts/STHeiti Medium.ttc'),
                      Path('C:/Windows/Fonts/msyh.ttc'),
                      Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')) if p.exists()), None)
    font = ImageFont.truetype(str(font_path),21) if font_path else ImageFont.load_default(size=19)
    small = ImageFont.truetype(str(font_path),17) if font_path else font
    def title(zh, en):
        return zh if font_path else en

    worm = rows[0]['worm_id']
    tracks = load_tracks(summary['sample'],worm,source)
    video = cv2.VideoCapture(str(source['video']))
    tmp = folder/'tracking.tmp.mp4'
    height = 2*(PANEL_H+BAR_HEIGHT)+FOOT_HEIGHT
    writer = imageio_ffmpeg.write_frames(str(tmp), (2*PANEL_W,height), fps=source['fps'], codec='libx264',
                                         pix_fmt_in='bgr24', macro_block_size=1,
                                         output_params=['-crf','16','-movflags','+faststart'])
    writer.send(None)
    origin = np.array(source['crop_xyxy'][:2])
    length = (summary['body_scale'] or {}).get('length_px', 250.)
    point_file = folder/'points.tmp.csv'
    totals = Counter()
    with point_file.open('w', encoding='utf-8-sig',newline='') as handle:
        table = csv.writer(handle)
        table.writerow(['frame','point_group','window','seed_frame','point_id','source_x_px','source_y_px','model_visible'])
        try:
            for i, rec in enumerate(records):
                ok, frame = video.read()
                if not ok:
                    raise RuntimeError(f'Undecodable source frame {i}')
                mask = get_mask(worm,i)
                contours, _ = cv2.findContours(mask.astype(np.uint8),cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
                outlines = [c.reshape(-1,2) for c in contours]
                curve = display.get((worm,i))
                if curve is not None and len(curve) != 101:
                    curve = sampled(curve)
                observed = tracks[i]
                tracked = [group[4] for groups in observed.values() for group in groups]
                body = outlines + ([curve] if curve is not None else [])
                factor, shift = camera(np.vstack(body or tracked),length)
                tf, ts = camera(np.vstack(body+tracked),length)
                raw = view(frame,origin,factor,shift)
                posture = raw.copy()
                cv2.drawContours(posture,[pixels(c,factor,shift) for c in contours],-1,(83,192,76),1,cv2.LINE_AA)
                if curve is not None:
                    pts = pixels(curve,factor,shift)
                    cv2.polylines(posture,[pts],False,(38,96,111),1,cv2.LINE_AA)
                    for xy in pts:
                        cv2.circle(posture,tuple(xy),2,(24,40,42),-1,cv2.LINE_AA)
                        cv2.circle(posture,tuple(xy),1,BODY_COLOR,-1,cv2.LINE_AA)
                    row_points(table,i,'centerline','','',range(101),curve,[None]*101,origin)
                    totals['centerline'] += len(pts)
                for which in ('first','second'):
                    label = rec[which+'_label']
                    if not label:
                        continue
                    xy = tuple(pixels(np.array([rec[which+'_x'],rec[which+'_y']]),factor,shift))
                    color = (48,83,242) if label.startswith(('A','H')) else (250,170,22)
                    cv2.circle(posture,xy,5,color,2,cv2.LINE_AA)
                    cv2.putText(posture,label,(xy[0]+7,xy[1]-6),0,.62,color,2,cv2.LINE_AA)
                bottom = view(frame,origin,tf,ts)
                panels = [raw,posture]
                n_center = len(curve) if curve is not None else 0
                titles = [title(f"原视频  {Path(source['video']).name}    第 {i} 帧    {rec['time_s']:.2f} 秒",
                                f"{Path(source['video']).name}   frame {i}   {rec['time_s']:.2f} s"),
                          title(f"SAM2 轮廓与中线   {n_center} / 101 点",
                                f"SAM2 contour and centerline   {n_center} / 101")]
                for direction, color in DIRECTION_COLOR.items():
                    panel = bottom.copy()
                    count, seen = 0,0
                    windows = {}
                    for window,anchor,kind,indices,positions,visible in observed[direction]:
                        wi = windows.setdefault(window, len(windows))
                        projected = pixels(positions,tf,ts)
                        if (projected.min(0)<0).any() or (projected.max(0)>=[PANEL_W,PANEL_H]).any():
                            raise ValueError('Tracked point falls outside the rendered field')
                        for xy, vis in zip(projected,visible):
                            # Window boundaries can contain coincident queries. A second
                            # window gets a larger marker so both groups remain visible.
                            point = tuple(xy)
                            radius = 3+3*wi
                            cv2.circle(panel,point,radius+1,(32,38,36),1,cv2.LINE_AA)
                            if vis and wi:
                                cv2.circle(panel,point,radius,color,2,cv2.LINE_AA)
                                cv2.circle(panel,point,1,color,-1,cv2.LINE_AA)
                            else:
                                cv2.circle(panel,point,radius,color,-1 if vis else 1,cv2.LINE_AA)
                        count += len(projected)
                        seen += int(visible.sum())
                        group = direction if kind=='query' else direction+'_support'
                        row_points(table,i,group,window,anchor,indices,positions,visible,origin)
                        totals[group] += len(projected)
                    zh = '正向' if direction=='forward' else '反向'
                    titles.append(title(f"TAPNext++ {zh}   {count} 点 · {seen} 可见",
                                        f"TAPNext++ {direction}    {seen}/{count} visible"))
                    panels.append(panel)
                canvas = Image.new('RGB',(2*PANEL_W,height),(25,32,37))
                draw = ImageDraw.Draw(canvas)
                for j,(panel,caption) in enumerate(zip(panels,titles)):
                    ox,oy = (j%2)*PANEL_W,(j//2)*(PANEL_H+BAR_HEIGHT)
                    canvas.paste(Image.fromarray(cv2.cvtColor(panel,cv2.COLOR_BGR2RGB)),(ox,oy+BAR_HEIGHT))
                    draw.text((ox+12,oy+5),caption,font=font,fill=(231,239,242))
                segment = '—' if rec['block'] is None else str(rec['block']+1)
                footer = title(f"黄色为中线采样点    跟踪点实心为可见、空心为不可见    窗口交界显示两组点    |    片段 {segment}    A/B 局部顺序    H?/T? 锥度推定",
                               f"Yellow: midline   Filled: visible   Hollow: invisible   Shared frames show both windows   |   Segment {segment}   A/B local   H?/T? taper")
                if rec.get('orientation') == 'manual_seed_temporal':
                    footer = title("黄色为中线采样点    跟踪点实心为可见、空心为不可见    |    红 H 头、蓝 T 尾，沿首帧人工标注推定",
                                   "Yellow: centerline   Filled: visible   Hollow: invisible   |   H/T tracked from the manual frame-zero head")
                if rec.get('link') == 'query_order_and_velocity':
                    footer += title("    本帧按跟踪点序校正", "    Query-order correction")
                draw.text((12,height-35),footer,font=small,fill=(224,231,233))
                writer.send(np.ascontiguousarray(cv2.cvtColor(np.asarray(canvas),cv2.COLOR_RGB2BGR)))
                if i==0:
                    canvas.save(folder/'first-frame.jpg',quality=94)
            if video.read()[0]:
                raise ValueError('Source contains more frames than the tracking run')
        finally:
            writer.close()
            video.release()
    tmp.replace(folder/'tracking.mp4')
    point_file.replace(folder/'points.csv')
    return dict(totals)


def run(sample):
    data, source, ids, get_mask, _ = load_sample(sample, fitted_mask_source(sample,'centerlines-tapnext'))
    if len(ids) != 1:
        raise ValueError('Select a single-worm run')
    worm = ids[0]
    folder = ROOT/'outputs'/sample/'phenotypes-centerlines-tapnext'
    rows = json.loads((folder/'per_frame_metrics.json').read_text())
    with np.load(folder/'display-centerlines.npz', allow_pickle=False) as saved:
        display = {tuple(int(v[1:]) for v in key.split('_')):saved[key] for key in saved}
    fill_display_gaps(rows,display,get_mask)
    guards = load_guards(data,ids,len(rows),required=True)[worm]
    out = folder/'size-adaptive'
    records, summary = review_output(out, rows, display, guards, get_mask, source, sample)
    summary['rendered_points'] = render(out,source,rows,display,records,summary,get_mask)
    (out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({k:v for k,v in summary.items() if k!='intervals'}),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', required=True)
    run(parser.parse_args().sample)
