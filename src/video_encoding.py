"""Stream BGR frames to a browser-compatible MP4 using the active backend."""
from contextlib import contextmanager
from functools import lru_cache
import subprocess
import tempfile

import imageio_ffmpeg

from platforms.device import selected_device


@lru_cache(maxsize=16)
def _nvenc_available(ffmpeg, width, height):
    # The FFmpeg build can list NVENC while the driver or frame size rejects it.
    result = subprocess.run([
        ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
        f'color=size={width}x{height}:rate=1', '-frames:v', '1',
        '-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p', '-f', 'null', '-',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        print('NVENC 无法编码该尺寸，切换到软件编码。', flush=True)
    return result.returncode == 0


@contextmanager
def h264_writer(path, size, fps, *, audio_source=None):
    """Yield a binary stream accepting one contiguous BGR image per frame."""
    width, height = size
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cuda = selected_device() == 'cuda'
    if cuda and _nvenc_available(ffmpeg, width + width % 2, height + height % 2):
        codec = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-rc', 'vbr', '-cq', '18', '-b:v', '0']
    else:
        codec = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-threads', '4']
    print(f'H.264 视频编码器 {codec[1]}', flush=True)
    command = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
               '-pixel_format', 'bgr24', '-video_size', f'{width}x{height}',
               '-framerate', str(fps), '-i', 'pipe:0']
    if audio_source:
        command += ['-i', str(audio_source), '-map', '0:v', '-map', '1:a?', '-c:a', 'aac']
    command += ['-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', *codec, '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', str(path)]
    with tempfile.TemporaryFile() as log, subprocess.Popen(
            command, stdin=subprocess.PIPE, stderr=log) as encoder:
        try:
            yield encoder.stdin
            encoder.stdin.close()
            if encoder.wait():
                raise BrokenPipeError()
        except BrokenPipeError as exc:
            encoder.wait()
            log.seek(0)
            raise RuntimeError('视频编码失败 ' + log.read().decode(errors='replace')[-600:]) from exc
        except BaseException:
            encoder.kill()
            raise
