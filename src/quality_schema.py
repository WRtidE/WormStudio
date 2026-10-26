"""One measurement contract for computation, CSV/JSON, HTTP and video overlays.

Legacy files remain readable, explicitly marked unassessed. New records fail
closed when a dimension is missing. See docs/QUALITY_SCHEMA.md for semantics.
"""
import json
import math

VERSION = 'wormstudio-quality-v2'
DIMENSIONS = ('source', 'mask', 'pose', 'identity', 'head', 'scale')


def flag(row, key, fallback=False):
    value = row.get(key, fallback)
    if value is None or value == '':value=fallback
    return value is True or str(value).lower() == 'true'


def dimension(row, name, valid, status, reason, evidence):
    row[name + '_valid'] = bool(valid)
    row[name + '_status'] = status
    row[name + '_reason'] = reason
    row[name + '_evidence'] = evidence if isinstance(evidence, str) else json.dumps(evidence, ensure_ascii=False, sort_keys=True)


def permissions(row):
    legacy = not row.get('quality_schema_version')
    if not legacy and row['quality_schema_version'] not in (VERSION, 'wormstudio-quality-v1'):
        return dict.fromkeys(('area','posture','motion','head','volume'),False)
    source = flag(row, 'source_valid', legacy)
    mask = source and flag(row, 'mask_valid')
    pose = mask and flag(row, 'pose_valid', flag(row, 'centerline_valid', flag(row, 'valid_for_summary')) if legacy else False)
    identity = source and flag(row, 'identity_valid', legacy)
    scale = flag(row, 'scale_valid', legacy)
    head = flag(row, 'head_valid', flag(row, 'head_metrics_valid') if legacy else False)
    if row.get('quality_schema_version') == VERSION:
        head = head and flag(row, 'head_identity_confirmed')
    scale = scale or row.get('measurement_mode') == 'pixels'
    time_ok = flag(row, 'time_valid', True)
    return dict(area=mask and scale, posture=pose and identity and scale,
                motion=mask and identity and scale and time_ok,
                head=pose and identity and head and time_ok,
                volume=pose and identity and scale and flag(row, 'volume_valid'))


def describe(row):
    legacy = not row.get('quality_schema_version')
    fallbacks = dict(source=True, mask=flag(row, 'mask_valid'),
        pose=flag(row, 'centerline_valid', flag(row, 'valid_for_summary')),
        identity=False, head=flag(row, 'head_metrics_valid'), scale=False)
    return dict(version=row.get('quality_schema_version', 'legacy-unassessed'),
        legacy=legacy, dimensions={name: dict(
            valid=flag(row, name+'_valid', fallbacks[name] if legacy else False),
            status=row.get(name+'_status', 'legacy_unassessed' if legacy else 'missing'),
            reason=row.get(name+'_reason', 'not_recorded'),
            evidence=row.get(name+'_evidence', 'legacy CSV; dimensions not independently assessed'))
            for name in DIMENSIONS}, permissions=permissions(row))


def tracking_dimensions(row, guard, scale, scale_info):
    row['quality_schema_version'] = VERSION
    source_ok = not guard.get('source_frame_invalid', False)
    dimension(row, 'source', source_ok, 'not_excluded' if source_ok else 'excluded',
              '' if source_ok else 'invalid_decoded_source_frame', guard.get('source_evidence', 'decoded tracking input; no automatic corruption detector'))
    dimension(row, 'mask', source_ok and guard['mask_valid'], 'passed' if guard['mask_valid'] else 'rejected',
              ';'.join(guard['reasons']), dict(rule='multiworm_quality', frame=guard['frame'],
              contact_ids=guard.get('contact_ids', []), area_px=guard.get('area_px'),
              fragmented=guard.get('fragmented'), boundary_truncated=guard.get('boundary_truncated')))
    dimension(row, 'identity', source_ok and guard.get('identity_valid', not guard['identity_review']),
              guard['identity_status'], guard.get('identity_reason', 'continuity_is_provisional'),
              guard.get('identity_evidence', 'SAM2 seed + continuity guards; not independent biological truth'))
    row['identity_segment'] = guard.get('identity_segment', 0)
    for field in ('identity_link_id','identity_link_source','identity_human_confirmed','identity_original_valid'):
        if field in guard:row[field]=guard[field]
    row['contact_ids'] = json.dumps(guard.get('contact_ids', []))
    row['measurement_mode'] = 'pixels' if scale_info.get('status') == 'pixels' else 'physical'
    scale_ok = bool(math.isfinite(scale) and scale > 0 and scale_info.get('status','supplied') in ('supplied','confirmed','user_supplied'))
    pixels=row['measurement_mode']=='pixels'
    dimension(row, 'scale', scale_ok, 'pixels' if pixels else 'supplied' if scale_ok else 'unverified_estimate',
              'physical_scale_not_supplied' if pixels else '' if scale_ok else 'scale_not_verified', scale_info)


def finish_dimensions(row):
    dimension(row, 'pose', flag(row, 'centerline_valid'), 'passed' if flag(row, 'centerline_valid') else 'rejected',
              row.get('quality_reason', ''), dict(candidate=row.get('centerline_candidate'), topology=row.get('centerline_topology'),
              inside_fraction=row.get('centerline_inside_fraction')))
    head_ok = flag(row, 'head_metrics_valid')
    row.setdefault('head_detection_status',row.get('head_status','unconfirmed_A_B_endpoints'))
    candidate=row['head_detection_status'] in ('unconfirmed_twardis_motion_candidate','unconfirmed_taper_candidate','unconfirmed_relative_motion_candidate','unconfirmed_appearance_candidate','unconfirmed_temporal_candidate')
    dimension(row, 'head', head_ok, 'confirmed' if head_ok and flag(row, 'head_identity_confirmed') else 'candidate' if candidate else 'unknown',
              row.get('head_reason', ''), dict(method=row.get('head_status'), score=row.get('head_evidence_score'), segment=row.get('head_segment')))
    allowed = permissions(row)
    row['valid_for_summary'] = allowed['posture']
    row['trajectory_valid'] = allowed['motion']
    row['quality_reason'] = ';'.join(dict.fromkeys(filter(None, [row.get('quality_reason', ''),
        row.get('identity_reason', '') if not flag(row, 'identity_valid') else '',
        row.get('scale_reason', '') if not flag(row, 'scale_valid') and row.get('measurement_mode')!='pixels' else ''])))
    return allowed


METRIC_GROUPS = dict(area=('area_um2',), posture=('length_um', 'mean_width_um','endpoint_a_angle_deg','opposite_end_angle_deg'),
    volume=('volume_estimate_um3',), motion=('centroid_x_um', 'centroid_y_um', 'speed_um_s', 'speed_bl_s',
        'centroid_x_source_px','centroid_y_source_px','centroid_smoothed_x_um','centroid_smoothed_y_um'),
    head=('head_candidate_angle_deg', 'head_candidate_angle_smoothed_deg', 'head_x_crop_px', 'head_y_crop_px',
          'tail_x_crop_px', 'tail_y_crop_px', 'heading_x', 'heading_y'))


def exclude_unusable(row):
    """Return raw diagnostic values; primary measurements use NaN (CSV blank/JSON null)."""
    allowed = permissions(row)
    candidate = dict(frame=row['frame'], worm_id=row['worm_id'])
    for group, fields in METRIC_GROUPS.items():
        for key in fields:
            if row.get('measurement_mode') == 'pixels': key = key.replace('_um','_px')
            candidate[key] = row.get(key)
            if not allowed[group] and key in row:
                row[key] = float('nan')
    if not allowed['motion']:
        row['movement_state'] = 'unknown'
    return candidate
