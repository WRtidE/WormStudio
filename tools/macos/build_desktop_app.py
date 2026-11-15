"""Install the Electron Finder launcher for this checkout on Apple Silicon."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
from electron_desktop import verified_archive


def build(archive, destination):
    verified_archive(archive, 'darwin-arm64')
    python = ROOT/'.venv/bin/python'
    if not python.is_file():
        raise FileNotFoundError(python)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        tmp = Path(tmp)
        subprocess.run(['ditto', '-x', '-k', archive, tmp], check=True)
        bundle = tmp/'Electron.app'
        contents = bundle/'Contents'
        plist = contents/'Info.plist'
        info = plistlib.loads(plist.read_bytes())
        info.update(CFBundleDisplayName='WormStudio', CFBundleName='WormStudio',
                    CFBundleIdentifier='org.wormstudio.desktop', CFBundleShortVersionString=json.loads((ROOT/'tools/electron/package.json').read_text())['version'],
                    LSMinimumSystemVersion='14.0')
        plist.write_bytes(plistlib.dumps(info))
        res = contents/'Resources'
        with Image.open(ROOT/'web/wormstudio-icon.png') as image:
            image.save(res/info['CFBundleIconFile'], format='ICNS')
        for name in ['LICENSE', 'LICENSES.chromium.html']:
            shutil.copy2(tmp/name, res/name)
        app = res/'app'
        shutil.copytree(ROOT/'tools/electron', app)
        (app/'local.json').write_text(json.dumps(dict(workspace=str(ROOT), python=str(python))))
        subprocess.run(['codesign', '--force', '--deep', '--sign', '-', bundle], check=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', bundle], check=True)
        backup = tmp/'previous.app'
        if destination.exists():
            old = plistlib.loads((destination/'Contents/Info.plist').read_bytes())
            if old.get('CFBundleIdentifier') != 'org.wormstudio.desktop':
                raise ValueError(f'目标路径属于其他应用：{destination}')
            os.rename(destination, backup)
        try:
            bundle.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
    print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--destination', type=Path, default=Path.home()/'Applications/WormStudio.app')
    args = parser.parse_args()
    build(args.archive, args.destination)
