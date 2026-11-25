"""Build the Apple Silicon app or portable Windows x64 folder from verified native binaries.

Run on an Apple Silicon build host with CPython 3.11, pip, packaging and Pillow.
The cache contains python-{mac,win}.tar.gz, electron-{mac,win}.zip and native
wheels in wheels-{mac,win}. Windows also uses Microsoft's Visual C++ runtime.
"""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from PIL import Image
from packaging.utils import canonicalize_name, parse_wheel_filename

HERE = Path(__file__).resolve().parent
RUNTIME_HASH = {
    'mac': '9d50cd14269d1ea8601ab279ae0175bdaad7cb151700f53f201a1ea7d0dc53b7',
    'win': 'ec6d6439f700863e2ea740c4e7b80d70cbc3a605a8d3d8b9b817626317ef2621',
}

def install_plot_font(cache, payload):
    base = 'https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/'
    files = {
        'OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf': '2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b',
        'LICENSE': '6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2',
    }
    fonts = cache / 'font'
    fonts.mkdir(exist_ok=True)
    dest = payload / 'fonts'
    dest.mkdir()
    for remote, digest in files.items():
        cached = fonts / Path(remote).name
        if not cached.is_file():
            with tempfile.TemporaryDirectory(dir=fonts) as temp:
                download = Path(temp) / cached.name
                urllib.request.urlretrieve(base + remote, download)
                verify(download, digest)
                download.replace(cached)
        verify(cached, digest)
        shutil.copy2(cached, dest / cached.name)

def verify(file, expected):
    with file.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise ValueError(f'Checksum mismatch: {file}')

def build(system, source, cache, sam, weights, examples, stage):
    sys.path.insert(0, str(source/'src'))
    from electron_desktop import verified_archive
    from resource_paths import release_examples
    videos = release_examples(examples)
    archive = cache/f'electron-{system}.zip'
    mac = system == 'mac'
    verified_archive(archive, 'darwin-arm64' if mac else 'win32-x64')
    tarball = cache/f'python-{system}.tar.gz'
    verify(tarball, RUNTIME_HASH[system])
    stage.mkdir(parents=True, exist_ok=False)
    app = stage/('WormStudio.app' if mac else 'WormStudio')
    if mac:
        subprocess.run(['ditto','-x','-k',archive,stage],check=True)
        (stage/'Electron.app').rename(app)
        contents = app/'Contents'
        resources = contents/'Resources'
        plist = contents/'Info.plist'
        info = plistlib.loads(plist.read_bytes())
        info.update(CFBundleName='WormStudio', CFBundleDisplayName='WormStudio',
                    CFBundleIdentifier='org.wormstudio.desktop', LSMinimumSystemVersion='14.0',
                    CFBundleShortVersionString=json.loads((source/'tools/electron/package.json').read_text())['version'])
        plist.write_bytes(plistlib.dumps(info))
        with Image.open(source/'web/wormstudio-icon.png') as image:
            image.save(resources/info['CFBundleIconFile'], format='ICNS')
        for name in ('LICENSE','LICENSES.chromium.html'):
            (stage/name).rename(resources/name)
    else:
        with zipfile.ZipFile(archive) as runtime:
            runtime.extractall(app)
        (app/'electron.exe').rename(app/'WormStudio.exe')
        from platforms.executable_icon import set_executable_icon
        set_executable_icon(app/'WormStudio.exe', source/'tools/electron/WormStudio.ico')
        resources = app/'resources'
    entry = resources/'app'
    entry.mkdir()
    config = json.loads((source/'tools/electron/package.json').read_text())
    config.update(main='desktop.cjs', name='WormStudio')
    (entry/'package.json').write_text(json.dumps(config))
    shutil.copy2(HERE/'desktop.cjs', entry)
    payload = resources/'analysis'
    payload.mkdir()
    install_plot_font(cache, payload)
    with tarfile.open(tarball) as runtime:
        runtime.extractall(payload, filter='data')
    python = payload/'python'
    site = python/('lib/python3.11/site-packages' if mac else 'Lib/site-packages')
    shutil.rmtree(site, ignore_errors=True)
    wheels = sorted((cache/f'wheels-{system}').glob('*.whl'))
    pins = [s.split('==') for s in (source/'tools/macos/pins.txt').read_text().splitlines() if s and not s.startswith('#')]
    expected = {canonicalize_name(name):version for name, version in pins}
    expected['setuptools'] = '84.2.1'
    if not mac:
        expected.update(colorama='0.4.6',pywin32='311')
    installed = {str(name):str(version) for name,version,_,_ in map(parse_wheel_filename,[w.name for w in wheels])}
    if installed != expected:
        raise ValueError(f'Wheel set differs from lock file: {installed.items() ^ expected.items()}')
    options = [] if mac else ['--platform','win_amd64','--implementation','cp','--python-version','3.11','--abi','cp311']
    subprocess.run([sys.executable,'-m','pip','--disable-pip-version-check','install','--no-index','--no-compile',
                    '--no-deps','--only-binary=:all:','--target',site,*options,*wheels], check=True)
    for record in site.glob('*.dist-info/direct_url.json'):
        record.unlink()
    shutil.rmtree(site/'bin', ignore_errors=True)
    if not mac:
        # Make native C++ dependencies available even on a clean Windows machine.
        for dll in (cache/'vc-runtime').glob('*.dll'):
            shutil.copy2(dll, python)
        if not (python/'msvcp140.dll').is_file():
            raise FileNotFoundError('Missing MSVC runtime')
    code = payload/'code'
    manifest = json.loads((source/'SOURCE_MANIFEST.json').read_text())
    for name, digest in manifest['files'].items():
        rel = Path(name)
        if (not mac and name == 'tools/release/windows_gpu.py') or rel.parts[0] in ('src','web','config','packages','docs') or name.startswith('tools/electron/') or name in (
            'studio.py','worm_pipeline.py','evaluation.py','review_saved_result.py','THIRD_PARTY.md'):
            verify(source/rel, digest)
            (code/rel).parent.mkdir(exist_ok=True,parents=True)
            shutil.copy2(source/rel,code/rel)
    (code/'examples').mkdir()
    for video in videos:
        shutil.copy2(video, code/'examples'/video.name)
    samroot = payload/'sam2'
    shutil.copytree(sam/'sam2',samroot/'sam2',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.so','*.pyd'))
    for licensefile in sam.glob('LICENSE*'):
        shutil.copy2(licensefile,samroot)
    record = json.loads((code/'docs/models.json').read_text())['sam2']
    verify(weights/record['filename'],record['sha256'])
    (payload/'models').mkdir()
    shutil.copy2(weights/record['filename'],payload/'models'/record['filename'])
    if mac:
        subprocess.run(['codesign','--force','--deep','--sign','-',app], check=True)
        subprocess.run(['codesign','--verify','--deep','--strict',app], check=True)
    print(app)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('system', choices=['mac','win'])
    for key in ('source','cache','sam','weights','examples','stage'):
        parser.add_argument(key, type=lambda s:Path(s).resolve())
    build(**vars(parser.parse_args()))
