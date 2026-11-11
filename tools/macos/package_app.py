"""Write a self-contained Finder app ZIP without running foreign executables."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import plistlib
import stat
import struct
import sys
import tarfile
import zipfile

from packaging.utils import parse_wheel_filename, canonicalize_name
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from export_source import ROOT, source_files

APP = 'WormStudio.app/Contents/'
RES = APP + 'Resources/'
PYTHON_HASH = '9d50cd14269d1ea8601ab279ae0175bdaad7cb151700f53f201a1ea7d0dc53b7'


def icon():
    im = Image.new('RGBA', (512, 512))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((16,16,496,496), radius=108, fill='#164b48')
    points = []
    import math
    for i in range(320):
        points.append((96+i, 258 + 85*math.sin((i-35)*math.pi/150)))
    d.line(points, fill='#9de5bd', width=27)
    for x,y in [points[0],points[-1]]:
        d.ellipse((x-13,y-13,x+13,y+13),fill='#9de5bd')
    x,y=points[-1]
    d.ellipse((x-7,y-7,x+7,y+7),fill='#fff5c8')
    parts = []
    for tag, size in [(b'icp4',16),(b'icp5',32),(b'icp6',64),(b'ic07',128),(b'ic08',256),(b'ic09',512)]:
        out = io.BytesIO()
        im.resize((size,size),Image.Resampling.LANCZOS).save(out,'PNG')
        parts.append(tag + struct.pack('>I',len(out.getvalue())+8) + out.getvalue())
    data = b''.join(parts)
    return b'icns' + struct.pack('>I',len(data)+8) + data


def build(cache, models, sam, example, destination):
    from fetch_wheels import rank
    pins = {canonicalize_name(p.split('==')[0]):p.split('==')[1]
            for p in (HERE/'pins.txt').read_text().splitlines() if p and not p.startswith('#')}
    wheels = {}
    requirements = []
    for path in sorted((cache/'wheels').glob('*.whl')):
        name, version, _, tags = parse_wheel_filename(path.name)
        if name in wheels or pins.get(name) != str(version) or not tags.intersection(rank):
            raise ValueError('Unexpected wheel: '+path.name)
        with path.open('rb') as fh:
            sha = hashlib.file_digest(fh, 'sha256').hexdigest()
        requirements.append(f'{name}=={version} --hash=sha256:{sha}')
        wheels[name] = path
    if set(wheels) != set(pins): raise ValueError(f'Missing wheels: {set(pins)-set(wheels)}')
    runtime = cache/'python.tar.gz'
    with runtime.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != PYTHON_HASH: raise ValueError('Wrong Mac Python archive')
    manifest = json.loads((ROOT/'docs/models.json').read_text())
    if not (sam/'sam2/configs/sam2/sam2_hiera_l.yaml').is_file(): raise ValueError('Missing SAM2 config')
    if destination.exists(): raise FileExistsError(destination)
    temp = destination.with_suffix('.partial')
    with zipfile.ZipFile(temp, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        def put(name, data, mode=0o644, stored=False):
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG|mode) << 16
            entry.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            z.writestr(entry, data)
        put(APP+'Info.plist', plistlib.dumps(dict(CFBundleExecutable='WormStudio', CFBundleName='WormStudio',
            CFBundleDisplayName='WormStudio', CFBundlePackageType='APPL', CFBundleVersion='1',
            CFBundleIdentifier='org.wormstudio.desktop', CFBundleShortVersionString='1.0',
            CFBundleIconFile='WormStudio.icns', LSMinimumSystemVersion='14.0', NSHighResolutionCapable=True)))
        put(RES+'WormStudio.icns', icon())
        put(APP+'MacOS/WormStudio', (HERE/'WormStudio.sh').read_text(encoding='utf-8').encode(), 0o755)
        put(RES+'desktop.py', (HERE/'desktop.py').read_bytes())
        put('先读我.txt', (HERE/'先读我.txt').read_bytes())
        put(RES+'requirements.lock', ('\n'.join(requirements)+'\n').encode())
        for path in wheels.values():
            z.write(path, RES+'wheels/'+path.name, zipfile.ZIP_STORED)
        # Materialize the handful of Python aliases as regular files for Finder and
        # other ZIP extractors. Keep all dylibs, licences and original POSIX modes.
        with tarfile.open(runtime) as tar:
            for m in tar.getmembers():
                if m.isdir(): continue
                if not m.name.startswith('python/') or '..' in Path(m.name).parts: raise ValueError(m.name)
                with tar.extractfile(m) as handle:
                    put(RES+m.name, handle.read(), 0o755 if m.mode & 0o111 else 0o644)
        for path in source_files():
            rel = path.relative_to(ROOT)
            if rel.parts[0] in ('web','src','packages','config') or rel.name in ('studio.py','worm_pipeline.py','evaluation.py','THIRD_PARTY.md'):
                z.write(path, RES+'app/'+rel.as_posix())
        z.write(example, RES+'app/examples/practice.avi')
        for path in [sam/'LICENSE', *sorted((sam/'sam2').rglob('*'))]:
            if path.is_file() and '__pycache__' not in path.parts and (path.suffix in ('.py','.yaml','.cu') or path.name=='LICENSE'):
                z.write(path, RES+'sam2/'+path.relative_to(sam).as_posix())
        for record in manifest.values():
            path = models/record['filename']
            digest = hashlib.sha256()
            entry = zipfile.ZipInfo(RES+'models/'+path.name)
            entry.file_size = path.stat().st_size
            with z.open(entry,'w',force_zip64=True) as out, path.open('rb') as inp:
                while block := inp.read(8*1024**2):
                    digest.update(block); out.write(block)
            if digest.hexdigest() != record['sha256']: raise ValueError('Wrong model: '+path.name)
    with zipfile.ZipFile(temp) as z:
        if error := z.testzip(): raise ValueError('Bad CRC: '+error)
    temp.rename(destination)
    print(f'{destination} ({destination.stat().st_size:,} bytes)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('cache','models','sam','example','destination'): parser.add_argument(arg,type=Path)
    build(**vars(parser.parse_args()))
