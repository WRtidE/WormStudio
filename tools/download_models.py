"""Download official checkpoints and verify SHA-256; never overwrite a good file."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(2**20),b''):h.update(block)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',choices=['sam2','tapnext','all'],default='all')
    parser.add_argument('--output',type=Path,default=Path('models'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    records=json.loads((Path(__file__).resolve().parents[1]/'docs/models.json').read_text(encoding='utf-8'))
    for key,item in records.items():
        if args.model not in ('all',key):continue
        target=args.output/item['filename']
        if target.exists():
            if digest(target)==item['sha256']:print('Verified',target);continue
            raise RuntimeError(f'{target} exists but has a different hash; move it before retrying.')
        part=target.with_suffix(target.suffix+'.partial')
        if part.exists():raise RuntimeError(f'Partial download exists: {part}; remove it to restart.')
        print('Downloading',item['filename'],flush=True)
        with urlopen(item['url'],timeout=120) as response,part.open('xb') as out:
            while block:=response.read(2**20):out.write(block)
        if digest(part)!=item['sha256']:raise RuntimeError(f'SHA-256 mismatch: {part}')
        part.rename(target);print('Verified',target)


if __name__=='__main__':main()
