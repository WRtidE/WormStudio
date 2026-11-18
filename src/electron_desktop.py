"""Compatibility re-export; desktop shell ownership lives in platforms.shell."""
from platforms.shell import (BIN, HASHES, ROOT, VERSION, command, environment,
                             launch, open_view, runtime_dir, verified_archive)

__all__ = ['ROOT', 'VERSION', 'HASHES', 'BIN', 'verified_archive', 'runtime_dir',
           'command', 'environment', 'launch', 'open_view']
