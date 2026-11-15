"""Refit self-contact events from saved SAM2 masks, optionally using ordered poses.

Example:
    python tools/refit_self_contacts.py --sample outputs/SAMPLE --output outputs/SAMPLE/contact-review

Pass --poses previous.npz for ordered xy[frame,101,2] and endpoint_visible[frame,2]
in analysis-crop pixels. In masks-only mode the two terminal identities are A/B.
"""

import argparse
import csv
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "packages" / "single-worm-methods")
)
from wormstudio_single.self_contact import InsufficientOpenPoses, observe, reconstruct
from wormstudio_single.skeleton import extract_body_geometry

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analyze_phenotypes import read_image
from display_centerlines import mask_guided_review
from identity_reassociation import RULES, ImageEvidence, reassociate


def review_identity(meta, directory, worm, records, reviews):
    """Restore a single track only when independently re-detected in the source movie."""

    def get_mask(worm_id, frame):
        filename = directory / "masks" / f"worm-{worm_id}" / f"{frame:06}.png"
        return read_image(filename, gray=True) != 0

    provider = ImageEvidence(meta, get_mask)
    try:
        linked, report = reassociate(
            {worm: records}, meta, get_mask, reviews=reviews, evidence_provider=provider
        )
        rows = linked[worm]
        bridges = []
        for edge in report["edges"]:
            if edge["decision"] != "auto_reassociated":
                continue
            i = edge["start_frame"] - 1
            if (
                i < 3
                or i + 2 >= len(rows)
                or edge["reference_end_frame"] != i - 1
                or records[i]["reasons"] != ["centroid_jump"]
                or edge["end_frame"] < i + 2
                or edge["reference_start_frame"] > i - 3
            ):
                continue
            # The accepted right fragment excludes the jump itself. Require the original
            # image at the intervening frame and both neighbours to support a single target.
            evidence = provider.compare(
                worm, list(range(i - 3, i)), list(range(i, i + 3))
            )
            current = evidence["after"][0]
            if not (
                evidence["accepted"]
                and current["candidate_count"] == 1
                and current["candidates"][0]["overlap"]
                >= RULES["minimum_candidate_overlap"]
                and current["foreground_support"] >= RULES["minimum_foreground_support"]
            ):
                continue
            rows[i].update(
                mask_valid=True,
                identity_valid=True,
                identity_status="image_supported_jump",
                identity_review=False, identity_reason="",
                identity_evidence={"source": "original_video", "frame": i, "link_id": edge["id"]},
            )
            segment = rows[i - 1]["identity_segment"]
            for j in range(i, edge["end_frame"] + 1):
                rows[j]["identity_segment"] = segment
            bridges.append({"frame": i, "evidence": evidence})
        report["image_supported_jumps"] = bridges
        return rows, report
    finally:
        provider.close()


def read_masks(sample, worm, config):
    from crop_recovery import tracking_directory
    seg = tracking_directory(sample, config)
    meta = json.loads((seg / "summary.json").read_text(encoding='utf-8-sig'))
    if worm not in meta["worm_ids"]:
        raise ValueError(
            f"Unknown object {worm}; available objects: {meta['worm_ids']}"
        )
    n = meta["frames"]
    good = np.zeros(n, bool)
    segment = np.zeros(n, int)
    association = None
    recovered = np.zeros(n, bool)
    guards = seg / "quality-guards.json"
    records = None
    if guards.exists():
        all_records = json.loads(guards.read_text(encoding='utf-8-sig'))["worms"]
        records = all_records[str(worm)]
        if len(records) != n:
            raise ValueError("Mask guard frame count mismatch")
        raw = records
        if (
            meta["worm_ids"] == [worm]
            and Path(meta["video"]).is_file()
            and (
                config.get("identity_reviews")
                or any(not r["identity_valid"] for r in records)
            )
        ):
            records, association = review_identity(
                meta, seg, worm, records, config.get("identity_reviews", [])
            )
        elif len(meta["worm_ids"]) > 1:
            # Multi-object reviews must use all identities; automatic bridging is single-worm only.
            linked, association = reassociate(
                {int(k): v for k, v in all_records.items()}, meta, None,
                reviews=config.get("identity_reviews", []),
            )
            records = linked[worm]
        for i, r in enumerate(records):
            recovered[i] = (
                r["mask_valid"] and r["identity_valid"] and not raw[i]["identity_valid"]
            )
            good[i] = (
                r["mask_valid"]
                and r["identity_valid"]
                and not r["source_frame_invalid"]
                and not r["contact_ids"]
            )
            segment[i] = (
                r["identity_segment"] if r["identity_segment"] is not None else -1
            )
    else:
        with (seg / "tracking-quality.csv").open(newline="") as f:
            for r in csv.DictReader(f):
                if int(r["worm_id"]) == worm:
                    good[int(r["frame"])] = (
                        r["low_confidence"].lower() != "true"
                        and int(r["ambiguous_overlap_px"]) == 0
                    )
    frames = []
    for i in range(n):
        m = read_image(seg / "masks" / f"worm-{worm}" / f"{i:06}.png", gray=True)
        frames.append(observe(m != 0, good[i]))
    return meta, frames, segment, recovered, association, records


def initial_curves(frames, segments):
    xy = np.full((len(frames), 101, 2), np.nan)
    vis = np.zeros((len(frames), 2), bool)
    previous = None
    for i, f in enumerate(frames):
        if not f.valid or (i and segments[i] != segments[i - 1]):
            previous = None
        if not f.valid:
            continue
        shape, source = extract_body_geometry(f.mask)
        if source is None:
            continue
        p = shape["ordered"] + f.offset
        if (
            previous is not None
            and np.linalg.norm(p[::-1] - previous, axis=1).mean()
            < np.linalg.norm(p - previous, axis=1).mean()
        ):
            p = p[::-1]
        xy[i] = previous = p
        vis[i] = True
    return xy, vis


def read_appearance(meta, frames):
    video = cv2.VideoCapture(meta["video"])
    if not video.isOpened():
        return False
    n = len(frames)
    x, y, x1, y1 = meta["crop_xyxy"]
    h, w = y1 - y, x1 - x
    sample = set(np.linspace(0, n - 1, min(25, n), dtype=int).tolist())
    background = []
    try:
        for i, f in enumerate(frames):
            ok, img = video.read()
            if not ok:
                raise ValueError(f"Truncated source video at frame {i}")
            gray = cv2.cvtColor(img[y:y1, x:x1], cv2.COLOR_BGR2GRAY)
            if gray.shape != (h, w):
                raise ValueError("Video and mask crop disagree")
            if i in sample:
                background.append(gray)
            # Allow the foreground-supported tip extension to search beyond the mask.
            padding = max(8, int(np.ceil(4 * f.mask.sum() / max(1, len(f.ridge)))))
            a, b = np.maximum(0, f.offset - padding)
            c, d = np.minimum([w, h], f.offset + f.mask.shape[::-1] + padding)
            f.photo_offset = np.array([a, b])
            f.raw = gray[b:d, a:c].copy()
        if video.grab():
            raise ValueError("Source video and mask frame counts differ")
    finally:
        video.release()
    bg = np.median(np.stack(background), axis=0).astype(np.float32)
    for f in frames:
        a, b = f.photo_offset
        h, w = f.raw.shape
        f.residual = ndimage.gaussian_filter(bg[b : b + h, a : a + w] - f.raw, 0.9)
    return True


def fit_sample(sample, poses=None, worm=1, *, allow_unresolved=False):
    config_path = sample / "pipeline-config.json"
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    meta, frames, segments, recovered, association, records = read_masks(sample, worm, config)
    if poses:
        with np.load(poses, allow_pickle=False) as z:
            xy = z["xy"]
            vis = z["endpoint_visible"]
            if "frame" in z and not np.array_equal(z["frame"], np.arange(len(frames))):
                raise ValueError(
                    "Input pose frame indices differ from the segmentation"
                )
            if "crop_origin_xy" in z and not np.array_equal(
                z["crop_origin_xy"], meta["crop_xyxy"][:2]
            ):
                raise ValueError("Input poses refer to a different analysis crop")
    else:
        xy, vis = initial_curves(frames, segments)
    has_appearance = read_appearance(meta, frames)
    manual = config.get("head_annotations", {}).get(str(worm), [])
    if isinstance(manual, dict):
        manual = manual.get("anchors", [manual])
    protected = [a["frame"] for a in manual if isinstance(a, dict) and "frame" in a]
    try:
        lines, seen, status, events, scale = reconstruct(
            frames, xy, vis, segments, protected, mask_guided_review
        )
    except InsufficientOpenPoses:
        if not allow_unresolved:
            raise
        lines, seen = xy, np.zeros_like(vis)
        status = np.array(["review" if f.valid else "invalid_mask" for f in frames])
        events = [{"first": 0, "last": len(frames)-1, "reason": "insufficient_open_poses"}]
        scale = {"length_px": None, "width_px": None}
    report = {
        "sample": sample.name,
        "worm": worm,
        "frames": len(frames),
        "fps": meta["fps"],
        "crop_xyxy": meta["crop_xyxy"],
        "order": "input" if poses else "A/B",
        "scale": scale,
        "raw_video_used": has_appearance,
        "status_counts": dict(Counter(status.tolist())),
        "events": events,
    }
    if association is not None:
        report["identity_reassociation"] = association
    pose = dict(xy=lines, endpoint_visible=seen, status=status,
                identity_reassociated=recovered, identity_segment=segments,
                crop_origin_xy=meta["crop_xyxy"][:2], frame=np.arange(len(frames)))
    return pose, report, records


def run(sample, output, poses=None, worm=1):
    pose, report, _ = fit_sample(sample, poses, worm)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        temp = Path(tmp)
        np.savez_compressed(temp / "poses.npz", **pose)
        (temp / "events.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        for name in ("poses.npz", "events.json"):
            (temp / name).replace(output / name)
    print(sample.name, len(report["events"]), "intervals", report["status_counts"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--poses", type=Path)
    parser.add_argument("--worm", default=1, type=int)
    args = parser.parse_args()
    run(args.sample, args.output, args.poses, args.worm)
