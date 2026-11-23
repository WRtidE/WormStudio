"""Embed the application icon into Windows release executables."""
import ctypes
from pathlib import Path
import shutil
import struct
import subprocess
import sys


def set_executable_icon(executable, icon):
    executable = Path(executable).resolve()
    icon = Path(icon).resolve()
    if sys.platform != 'win32':
        editor = shutil.which('rcedit')
        if not editor:
            raise RuntimeError('Windows cross-builds require rcedit on PATH (with Wine); or build on Windows')
        subprocess.run([editor, str(executable), '--set-icon', str(icon)], check=True)
        return
    data = Path(icon).read_bytes()
    reserved, kind, count = struct.unpack_from('<HHH', data)
    if reserved or kind != 1 or not count or len(data) < 6 + count * 16:
        raise ValueError('Invalid ICO directory')
    images = []
    group = bytearray(data[:6])
    for index in range(count):
        entry = data[6 + index * 16:22 + index * 16]
        size, offset = struct.unpack_from('<II', entry, 8)
        if not size or offset < 6 + count * 16 or offset + size > len(data):
            raise ValueError('Invalid ICO image bounds')
        images.append(data[offset:offset + size])
        group.extend(entry[:12] + struct.pack('<H', index + 1))
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    kernel.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ptr, ctypes.c_uint]
    kernel.LoadLibraryExW.restype = ptr
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ptr, ptr, ptr, ptr)
    kernel.EnumResourceNamesW.argtypes = [ptr, ptr, callback_type, ptr]
    kernel.FreeLibrary.argtypes = [ptr]
    kernel.BeginUpdateResourceW.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
    kernel.BeginUpdateResourceW.restype = ptr
    kernel.UpdateResourceW.argtypes = [ptr, ptr, ptr, ctypes.c_ushort, ptr, ctypes.c_uint]
    kernel.EndUpdateResourceW.argtypes = [ptr, ctypes.c_int]
    names = []
    languages = set()
    language_callback = ctypes.WINFUNCTYPE(ctypes.c_int, ptr, ptr, ptr, ctypes.c_ushort, ptr)
    kernel.EnumResourceLanguagesW.argtypes = [ptr, ptr, ptr, language_callback, ptr]

    @language_callback
    def collect_language(module, resource_type, name, language, param):
        languages.add(language)
        return 1

    @callback_type
    def collect(module, resource_type, name, param):
        names.append(name if name <= 65535 else ctypes.wstring_at(name))
        kernel.EnumResourceLanguagesW(module, resource_type, name, collect_language, None)
        return 1

    module = kernel.LoadLibraryExW(str(executable), None, 2)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.EnumResourceNamesW(module, 14, collect, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.FreeLibrary(module)
    handle = kernel.BeginUpdateResourceW(str(executable), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for language in languages:
            for index, image in enumerate(images, 1):
                if not kernel.UpdateResourceW(handle, 3, index, language, image, len(image)):
                    raise ctypes.WinError(ctypes.get_last_error())
            for name in names:
                identifier = name if isinstance(name, int) else ctypes.cast(ctypes.c_wchar_p(name), ptr)
                blob = bytes(group)
                if not kernel.UpdateResourceW(handle, 14, identifier, language, blob, len(blob)):
                    raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.EndUpdateResourceW(handle, True)
        raise
    if not kernel.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())
