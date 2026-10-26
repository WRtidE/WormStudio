"""Area consistency must not let missing detections invalidate good frames."""
import numpy as np


def reference_area(areas):
    usable = np.asarray(areas, float)
    usable = usable[np.isfinite(usable) & (usable >= 100)]
    return float(np.median(usable)) if len(usable) else 1.


def mark_quality(rows, ids, intervals=()):
    for worm in ids:
        sub = sorted((r for r in rows if r['worm_id'] == worm), key=lambda r: r['frame'])
        areas = [r['area_px'] for r in sub]
        reference = reference_area(areas)
        for i, row in enumerate(sub):
            deviation = abs(row['area_px']/reference-1)
            jump = abs(row['area_px']/max(areas[max(0,i-1)],1)-1)
            row.update(area_deviation_fraction=float(deviation), area_jump_fraction=float(jump),
                low_confidence=bool(row['ambiguous_overlap_px'] > .01*reference
                    or deviation > .25 or jump > .2 or row['area_px'] < 100))
            if any(worm in item['worm_ids'] and item['frames'][0] <= row['frame'] <= item['frames'][1]
                   for item in intervals):
                row['low_confidence'] = True
