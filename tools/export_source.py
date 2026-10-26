"""Export an explicit source allowlist; never follow data links or copy weights."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = ('.gitignore', '.gitattributes', '.editorconfig', 'README.md', 'THIRD_PARTY.md',
       'CONTRIBUTING.md', 'environment.yml', 'requirements.txt', 'requirements-cuda.txt',
       'requirements-cpu.txt', 'requirements-sam2.txt', 'studio.py', 'worm_pipeline.py',
       'evaluation.py', 'review_saved_result.py', 'run_tests.py', 'start.bat')
DOCS = ('INSTALL.md', 'ARCHITECTURE.md', 'USER_GUIDE.md', 'MODELS.md', 'QUALITY_SCHEMA.md',
        'DEVELOPMENT.md', 'RELEASE.md', 'VALIDATION.md', 'models.json')
CONFIG = ('phenotype_config.json', 'motion_constraints.json', 'README.md',
          'target_videos.example.json', 'evaluation_dataset.example.json')


def source_files():
    files = [ROOT/n for n in TOP]
    files += [ROOT/'docs'/n for n in DOCS]
    files += [ROOT/'config'/n for n in CONFIG]
    for folder in ('src', 'web', 'tests', 'tools', 'packages', '.github'):
        base=ROOT/folder
        for p in sorted(base.rglob('*')):
            if any(part in ('__pycache__', '.git', '.pytest_cache') for part in p.relative_to(base).parts):continue
            if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
                raise ValueError(f'Source links are not allowed: {p.relative_to(ROOT)}')
            if p.is_file() and p.suffix not in ('.pyc', '.pyo'):
                if not p.resolve().is_relative_to(ROOT.resolve()):raise ValueError('Source escaped project root')
                files.append(p)
    return sorted(set(files))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New directory; existing directories are never overwritten')
    args=parser.parse_args();destination=args.output.resolve()
    if destination==ROOT or destination.is_relative_to(ROOT):raise ValueError('Export outside the application source directory')
    if destination.exists() or destination.with_suffix('.zip').exists():raise FileExistsError(destination)
    files=source_files()
    for p in files:
        if not p.is_file():raise FileNotFoundError(p)
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
