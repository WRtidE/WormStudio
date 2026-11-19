"""Export an explicit source allowlist; never follow data links or copy weights."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = ('.gitignore', '.gitattributes', '.editorconfig', 'README.md', 'CHANGELOG.md', 'THIRD_PARTY.md',
       'CONTRIBUTING.md', 'environment.yml', 'requirements.txt', 'requirements-cuda.txt',
       'requirements-cpu.txt', 'requirements-macos.txt', 'requirements-sam2.txt', 'studio.py', 'worm_pipeline.py',
       'evaluation.py', 'review_saved_result.py', 'run_tests.py', 'start.bat')
DOCS = ('INSTALL.md', 'ARCHITECTURE.md', 'USER_GUIDE.md', 'MODELS.md', 'QUALITY_SCHEMA.md',
        'DEVELOPMENT.md', 'RELEASE.md', 'VALIDATION.md', 'models.json', 'FIRST_ANALYSIS.html', 'PORTABLE_RELEASE.md', 'CPU_OPTIMIZATION.md', 'MACOS.md', 'MAC_FIRST_ANALYSIS.html', 'releases/v1.0.1.md',
        'screenshots/prepare-frame.png', 'screenshots/confirm-contour.png', 'screenshots/result-playback.png',
        'screenshots/result-summary.png', 'screenshots/phenotype-plots.png')
CONFIG = ('phenotype_config.json', 'motion_constraints.json', 'README.md',
          'target_videos.example.json', 'evaluation_dataset.example.json')


def source_files():
    files = [ROOT/n for n in TOP]
    files += [ROOT/'docs'/n for n in DOCS]
    files += [ROOT/'config'/n for n in CONFIG]
    folders = ('src', 'web', 'tests', 'tools', '.github')
    checkout = (ROOT / '.git').exists()
    if checkout:
        tracked = subprocess.check_output(['git', 'ls-files', '--cached', '-z'], cwd=ROOT)
        tracked_paths = {ROOT / name for name in tracked.decode('utf-8').split('\0') if name}
        for required in files:
            if required not in tracked_paths:
                raise ValueError(f'Required source is not tracked: {required.relative_to(ROOT)}')
        candidates = (p for p in tracked_paths if p.relative_to(ROOT).parts[0] in folders)
    else:
        candidates = (p for folder in folders for p in (ROOT / folder).rglob('*'))
    for p in candidates:
        rel = p.relative_to(ROOT)
        if any(part in ('__pycache__', '.git', '.pytest_cache') for part in rel.parts):
            continue
        if '.before-' in p.name or rel.as_posix() == 'tools/electron/local.json':
            continue
        if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
            raise ValueError(f'Source links are not allowed: {rel}')
        if checkout and not p.is_file():
            raise ValueError(f'Tracked source is missing or not a regular file: {rel}')
        if p.is_file() and p.suffix not in ('.pyc', '.pyo'):
            files.append(p)
    for p in files:
        if p.resolve() != p or not p.is_file():
            raise ValueError(f'Source must be a regular project file: {p.relative_to(ROOT)}')
    return sorted(set(files))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New directory; existing directories are never overwritten')
    args=parser.parse_args();destination=args.output.resolve()
    if destination==ROOT or destination.is_relative_to(ROOT):raise ValueError('Export outside the application source directory')
    if destination.exists() or destination.with_suffix('.zip').exists():raise FileExistsError(destination)
    files=source_files()
    destination.mkdir(parents=True)
    for p in files:
        target=destination/p.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    sys.path.insert(0,str(ROOT/'tools'))
    from check_source import check
    check(destination)
    records={p.relative_to(destination).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(destination.rglob('*')) if p.is_file()}
    manifest=dict(format_version=1,files=records,excluded='weights, videos, outputs, evaluation data, private settings, validation logs, archives and virtual environments')
    (destination/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    archive=destination.with_suffix('.zip')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(destination.rglob('*')):
            if p.is_file():z.write(p,Path(destination.name)/p.relative_to(destination))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for name,digest in records.items():
            assert hashlib.sha256(z.read(destination.name+'/'+name)).hexdigest()==digest
    print(json.dumps(dict(files=len(records)+1,source_bytes=sum(p.stat().st_size for p in destination.rglob('*') if p.is_file()),zip_bytes=archive.stat().st_size,output=str(destination)),ensure_ascii=False))


if __name__=='__main__':main()
