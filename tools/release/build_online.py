"""Split a verified portable desktop build into an online app and reusable components.

The input is a relocatable desktop build, never a development virtualenv. The
output directory is new. Publish its components unchanged to --base-url before
distributing the app; the manifest is bundled with the app, not fetched unsigned.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from stat import S_ISREG
import tarfile
import tomllib
from urllib.parse import urlsplit
from zipfile import ZipFile, ZIP_DEFLATED

MAX_PART = 1400 * 1024**2
TORCH_PREFIXES = ('torch', 'torchvision', 'torchgen', 'functorch')


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def runtime_file(name):
    parts = PurePosixPath(name).parts
    if '__pycache__' in parts or name.endswith(('.pyc', '/direct_url.json')):
        return False
    if name.endswith('.pth'):
        # Distribution-owned path hooks (e.g. setuptools) remain; the old GPU
        # overlay hook points outside this portable runtime and must not travel.
        if 'gpu' in PurePosixPath(name).name.lower():
            return False
    license_file = PurePosixPath(name).name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))
    return license_file or not ('torch/include/' in name or ('torch/lib/' in name and name.endswith('.lib')))


def is_torch(relative):
    return relative.split('/')[0].startswith(TORCH_PREFIXES)


def pack_parts(entries, output, prefix, base_url):
    """Entries are (archive name, size, opener, mode); stable order and metadata."""
    groups, group, size = [], [], 0
    for entry in sorted(entries, key=lambda e: e[0]):
        if entry[1] > MAX_PART:
            raise ValueError(f'Runtime file exceeds component size limit: {entry[0]}')
        if group and size + entry[1] > MAX_PART:
            groups.append((group, size)); group, size = [], 0
        group.append(entry); size += entry[1]
    if group:
        groups.append((group, size))
    assets = []
    for number, (group, unpacked_size) in enumerate(groups, 1):
        print(f'Packing {prefix} {number}/{len(groups)} ({unpacked_size/1024**2:.1f} MiB)', flush=True)
        temporary = output / f'{prefix}-{number}.tar.gz'
        with temporary.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0, compresslevel=6) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for name, size, opener, mode in group:
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = size, mode, 0
                    with opener() as stream:
                        archive.addfile(info, stream)
        sha = digest(temporary)
        final = output / f'{prefix}-{sha[:20]}.tar.gz'
        temporary.rename(final)
        packed_size = final.stat().st_size
        print(f'Built {final.name} ({packed_size/1024**2:.1f} MiB)', flush=True)
        label = {'common':'基础运行环境', 'cpu':'CPU 分析组件', 'cuda':'GPU 加速组件'}[prefix.split('-')[0]]
        assets.append(dict(label=f'{label} · {number}/{len(groups)}', url=base_url+'/'+final.name,
                           sha256=sha, size=packed_size, unpackedSize=unpacked_size))
    return assets


def tree_entries(root, relative):
    resolved_root = root.resolve()
    for file in (root / relative).rglob('*'):
        name = file.relative_to(root).as_posix()
        if not runtime_file(name):
            continue
        info = file.stat()
        if S_ISREG(info.st_mode):
            # Resolving package-internal symlinks makes each component relocatable.
            if not file.resolve().is_relative_to(resolved_root):
                raise ValueError(f'External runtime link: {name}')
            yield name, info.st_size, lambda file=file: file.open('rb'), info.st_mode & 0o777


def build(source, desktop, output, base_url, target='win32-x64', gpu_wheels=None):
    url = urlsplit(base_url)
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError('--base-url must be a public HTTPS directory')
    base_url = base_url.rstrip('/')
    mac = target == 'darwin-arm64'
    resources = desktop / ('Contents/Resources' if mac else 'resources')
    payload = resources / 'analysis'
    python_relative = 'python/bin/python3.11' if mac else 'python/python.exe'
    site = 'python/lib/python3.11/site-packages' if mac else 'python/Lib/site-packages'
    if not (payload/python_relative).is_file() or (payload/'python/pyvenv.cfg').exists():
        raise ValueError('Input must be a portable desktop build with bundled Python')
    metadata = list((payload/site).glob('torch-2.7.0*.dist-info/METADATA'))
    if len(metadata) != 1 or '+cu' in metadata[0].parent.name:
        raise ValueError('Build the common components from the PyTorch 2.7.0 CPU desktop')
    manifest = json.loads((source/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    model = json.loads((source/'docs/models.json').read_text(encoding='utf-8'))['sam2']
    checkpoint = payload/'models'/model['filename']
    if digest(checkpoint) != model['sha256']:
        raise ValueError('Model checksum mismatch')
    output.mkdir(parents=True, exist_ok=False)
    components = output/'components'; components.mkdir()
    common, cpu = [], []
    for directory in ('python', 'sam2', 'fonts'):
        for entry in tree_entries(payload, directory):
            name = entry[0]
            destination = cpu if name.startswith(site+'/') and is_torch(name[len(site)+1:]) else common
            destination.append(entry)
    common_assets = pack_parts(common, components, f'common-{target}', base_url)
    backends = {'cpu':pack_parts(cpu, components, f'cpu-{target}', base_url)}
    if gpu_wheels:
        if mac:
            raise ValueError('NVIDIA wheels are only supported by the Windows x64 build')
        lock = tomllib.loads((source/'uv.lock').read_text(encoding='utf-8'))
        selected = [p for p in lock['package'] if p['name'] in ('torch','torchvision') and p['version'].endswith('+cu128')]
        if len(selected) != 2:
            raise ValueError('Missing pinned CUDA packages in uv.lock')
        from contextlib import ExitStack
        with ExitStack() as stack:
            entries = []
            for package in selected:
                wheel = next(w for w in package['wheels'] if 'cp311-cp311-win_amd64.whl' in w['url'])
                from urllib.parse import unquote
                file = gpu_wheels / unquote(urlsplit(wheel['url']).path.rsplit('/',1)[1])
                if digest(file) != wheel['hash'].removeprefix('sha256:'):
                    raise ValueError(f'Wheel checksum mismatch: {file.name}')
                archive = stack.enter_context(ZipFile(file))
                for info in archive.infolist():
                    name = info.filename
                    if info.is_dir() or not runtime_file(name):
                        continue
                    if name.startswith('/') or '..' in PurePosixPath(name).parts or '\\' in name or '.data/' in name:
                        raise ValueError(f'Unexpected wheel layout: {name}')
                    entries.append((site+'/'+name, info.file_size, lambda a=archive,i=info:a.open(i), 0o644))
            backends['cuda'] = pack_parts(entries, components, f'cuda-{target}', base_url)
    model_name = f'model-{model["sha256"][:20]}.pt'
    shutil.copyfile(checkpoint, components/model_name)
    online_manifest = dict(schema=1, target=target, common=common_assets, backends=backends,
        model=dict(label='SAM2 分析模型', filename=model['filename'], size=checkpoint.stat().st_size,
                   sha256=model['sha256'], url=base_url+'/'+model_name))
    app = output/('WormStudio.app' if mac else 'WormStudio')
    def exclude(folder, names):
        if Path(folder) == resources:
            return {'analysis','app'} & set(names)
        return set()
    shutil.copytree(desktop, app, ignore=exclude, symlinks=True)
    app_resources = app/('Contents/Resources' if mac else 'resources')
    code = app_resources/'analysis/code'
    for name, expected in manifest['files'].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
            raise ValueError(f'Invalid source path: {name}')
        if relative.parts[0] in ('src','web','config','docs','packages') or name.startswith('tools/electron/') or name in (
                'studio.py','worm_pipeline.py','evaluation.py','review_saved_result.py','THIRD_PARTY.md','tools/release/windows_gpu.py'):
            file = source / name
            if digest(file) != expected:
                raise ValueError(f'Source checksum mismatch: {name}')
            destination = code / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, destination)
    example = payload/'code/examples/practice.avi'
    if example.is_file():
        (code/'examples').mkdir(exist_ok=True); shutil.copyfile(example, code/'examples/practice.avi')
    (app_resources/'analysis/online-manifest.json').write_text(json.dumps(online_manifest, ensure_ascii=False, indent=2),encoding='utf-8')
    entry = app_resources/'app'; entry.mkdir()
    config = json.loads((source/'tools/electron/package.json').read_text(encoding='utf-8'))
    config.update(name='WormStudio', main='desktop.cjs')
    (entry/'package.json').write_text(json.dumps(config),encoding='utf-8')
    shutil.copyfile(source/'tools/release/desktop.cjs', entry/'desktop.cjs')
    if mac:
        import subprocess
        import plistlib
        plist = app/'Contents/Info.plist'
        info = plistlib.loads(plist.read_bytes())
        info['CFBundleShortVersionString'] = config['version']
        plist.write_bytes(plistlib.dumps(info))
        subprocess.run(['codesign','--force','--deep','--sign','-',app],check=True)
    name = 'WormStudio-Mac-arm64.zip' if mac else 'WormStudio-Windows-x64.zip'
    if mac:
        subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',app,output/name],check=True)
    else:
        with ZipFile(output/name,'w',compression=ZIP_DEFLATED,compresslevel=6) as archive:
            for file in sorted(app.rglob('*')):
                if file.is_file():
                    archive.write(file,file.relative_to(output).as_posix())
    # Component hashes were computed while producing each immutable artifact.
    assets = [*common_assets, *(asset for group in backends.values() for asset in group), online_manifest['model']]
    checksums = [(name, digest(output/name)), *sorted((asset['url'].rsplit('/',1)[1], asset['sha256']) for asset in assets)]
    (output/'SHA256SUMS.txt').write_text(''.join(f'{sha}  {filename}\n' for filename, sha in checksums),encoding='utf-8')
    print(json.dumps(dict(app=str(app), package=str(output/name), bytes=(output/name).stat().st_size,
                         components=len(assets), base_url=base_url),ensure_ascii=False),flush=True)
    return online_manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('source','desktop','output'):
        parser.add_argument('--'+key,required=True,type=lambda value:Path(value).resolve())
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--target',choices=('win32-x64','darwin-arm64'),default='win32-x64')
    parser.add_argument('--gpu-wheels',type=lambda value:Path(value).resolve())
    build(**vars(parser.parse_args()))
