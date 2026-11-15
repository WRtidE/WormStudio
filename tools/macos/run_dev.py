"""Open the current checkout in the shared Electron desktop shell."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))
from electron_desktop import launch

if __name__ == '__main__':
    raise SystemExit(launch())
