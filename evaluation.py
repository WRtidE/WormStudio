"""Command-line tools for immutable evaluation dataset snapshots."""
from pathlib import Path
import argparse
import json
import sys

STUDIO = Path(__file__).resolve().parent
sys.path.insert(0, str(STUDIO/'src'))


def main():
    from evaluation_dataset import inventory, read_json, write_new, freeze_split, build_tasks, snapshot_baseline
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    scan = commands.add_parser('inventory')
    scan.add_argument('--video-root', action='append', required=True)
    scan.add_argument('--record-root', action='append', required=True)
    scan.add_argument('--out', type=Path, required=True)
    split = commands.add_parser('freeze')
    split.add_argument('--dataset', type=Path, required=True)
    split.add_argument('--metadata', type=Path)
    split.add_argument('--seed', default='wormstudio-stage2-20261019')
    tasks = commands.add_parser('tasks')
    tasks.add_argument('--dataset', type=Path, required=True)
    tasks.add_argument('--plan', type=Path, required=True)
    audit = commands.add_parser('verify')
    audit.add_argument('--dataset', type=Path, required=True)
    export = commands.add_parser('export')
    export.add_argument('--dataset', type=Path, required=True)
    export.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'inventory':
        result = inventory(args.video_root, args.record_root, args.out)
        print(json.dumps(result['summary'], ensure_ascii=False, indent=2))
    elif args.command == 'freeze':
        result = freeze_split(read_json(args.dataset/'manifest.json'), read_json(args.metadata) if args.metadata else None, args.seed)
        write_new(args.dataset/'splits.json', result)
        snapshot_baseline(STUDIO, args.dataset/'baseline.json')
        print(json.dumps(dict(counts=result['counts'], readiness=result['readiness']), ensure_ascii=False))
    elif args.command == 'tasks':
        result = build_tasks(read_json(args.dataset/'manifest.json'), read_json(args.dataset/'splits.json'), read_json(args.plan))
        write_new(args.dataset/'tasks.json', result)
        print(json.dumps(result['summary'], ensure_ascii=False))
    else:
        from evaluation_annotations import DatasetStore
        dataset = DatasetStore(args.dataset)
        result = dataset.export_truth(args.out) if args.command == 'export' else dataset.verify()
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    from resource_paths import ensure_runtime
    ensure_runtime()
    main()
