"""Collect pinned wheels for CPython 3.11 on macOS 14 arm64, on any build host."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import IncompleteRead
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from time import sleep

from packaging.tags import cpython_tags, compatible_tags, mac_platforms
from packaging.utils import parse_wheel_filename

platforms = list(mac_platforms((14, 0), "arm64"))
tags = list(cpython_tags((3, 11), abis=["cp311"], platforms=platforms))
tags.extend(compatible_tags((3, 11), "cp311", platforms))
rank = {tag: n for n, tag in enumerate(tags)}
PURE_SDISTS = {"antlr4-python3-runtime", "iopath", "proxy-tools"}


def download(asset, destination):
    size, pos = asset["size"], 0
    digest = hashlib.sha256()
    with destination.open("wb") as out:
        while pos < size:
            end = min(pos + 4 * 1024 * 1024, size) - 1
            for attempt in range(4):
                try:
                    request = Request(asset["url"], headers={"Range": f"bytes={pos}-{end}"})
                    with urlopen(request, timeout=45) as stream:
                        if stream.status == 200 and pos == 0:
                            data = stream.read()
                            if len(data) != size:
                                raise OSError("Incomplete download")
                        else:
                            if stream.headers.get("Content-Range") != f"bytes {pos}-{end}/{size}":
                                raise OSError("Unexpected range response")
                            try:
                                data = stream.read()
                            except IncompleteRead as exc:
                                data = exc.partial  # request the remainder from its exact offset
                            if not 0 < len(data) <= end - pos + 1:
                                raise OSError("Incomplete range")
                    break
                except (OSError, IncompleteRead):
                    if attempt == 3: raise
                    sleep(1 + attempt)
            out.write(data)
            digest.update(data)
            pos += len(data)
    if digest.hexdigest() != asset["digests"]["sha256"]:
        raise ValueError("PyPI checksum mismatch: " + destination.name)


def fetch(pin, output):
    name, version = pin.split("==")
    with urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as reply:
        assets = json.load(reply)["urls"]
    candidates = []
    for asset in assets:
        if asset["filename"].endswith(".whl"):
            matches = parse_wheel_filename(asset["filename"])[3].intersection(rank)
            if matches:
                candidates.append((min(rank[t] for t in matches), asset))
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        if candidates:
            asset = min(candidates, key=lambda item: item[0])[1]
            target = output / asset["filename"]
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == asset["digests"]["sha256"]:
                return
            wheel = Path(temp) / target.name
            download(asset, wheel)
        elif name in PURE_SDISTS:
            asset = next(a for a in assets if a["packagetype"] == "sdist")
            source = Path(temp) / asset["filename"]
            download(asset, source)
            subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
                            "--no-build-isolation", "-w", temp, str(source)], check=True)
            wheel, = Path(temp).glob("*-none-any.whl")
        else:
            raise ValueError(f"No compatible Mac wheel: {pin}")
        wheel.replace(output / wheel.name)
    print(pin, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("lock", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    pins = [x for x in args.lock.read_text().splitlines() if x and not x.startswith("#")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {pin: pool.submit(fetch, pin, args.output) for pin in pins}
        failures = []
        for pin, job in jobs.items():
            try: job.result()
            except Exception as exc: failures.append(f"{pin}: {exc}")
        if failures: raise SystemExit("\n".join(failures))
