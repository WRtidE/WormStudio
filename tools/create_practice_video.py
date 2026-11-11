"""Synthetic operation tutorial, not a biological accuracy benchmark."""
from pathlib import Path


def create(folder):
    import cv2
    import numpy as np
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/'practice.avi'
    if target.exists():
        raise FileExistsError(target)
    writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*'MJPG'), 5, (256, 256))
    if not writer.isOpened():
        raise RuntimeError('练习视频编码器不可用')
    try:
        for i in range(5):
            frame = np.full((256, 256, 3), 210, np.uint8)
            points = np.array([[x, 128+int(18*np.sin((x-50)/55+i*.1))] for x in range(48,208)], np.int32)
            cv2.polylines(frame, [points], False, (55,55,55), 12)
            writer.write(frame)
    finally:
        writer.release()
    capture = cv2.VideoCapture(str(target))
    count = 0
    try:
        while capture.read()[0]:
            count += 1
    finally:
        capture.release()
    if count != 5:
        raise RuntimeError('练习视频未通过解码校验')
