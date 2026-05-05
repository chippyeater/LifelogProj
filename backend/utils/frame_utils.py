import os
from typing import Optional

import cv2


def parse_timestamp_to_seconds(time_str: str) -> float:
    # 将 HH:MM:SS / MM:SS / SS 格式时间戳转换为秒数
    parts = (time_str or "").split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = map(float, parts)
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = map(float, parts)
            return minutes * 60 + seconds
        if len(parts) == 1:
            return float(parts[0])
    except ValueError as e:
        raise ValueError(f"Invalid timestamp format: {time_str}") from e
    raise ValueError(f"Unsupported timestamp format: {time_str}")


def timestamp_to_frame_index(time_str: str, fps: float) -> int:
    # 根据时间戳和 fps 计算目标帧号
    if fps <= 0:
        raise ValueError(f"Invalid fps: {fps}")
    return int(parse_timestamp_to_seconds(time_str) * fps)


def extract_frame_to_path(
    video_path: str,
    frame_time: str,
    output_path: str,
    *,
    cap: Optional[cv2.VideoCapture] = None,
    fps: Optional[float] = None,
    total_frames: Optional[int] = None,
) -> bool:
    # 统一的单帧抽取入口，支持外部复用同一个 VideoCapture
    if cap is None and (not video_path or not os.path.exists(video_path)):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    owns_capture = cap is None
    local_cap = cap or cv2.VideoCapture(video_path)
    if not local_cap.isOpened():
        if owns_capture:
            local_cap.release()
        raise RuntimeError(f"Failed to open video file: {video_path}")

    try:
        local_fps = fps if fps is not None else local_cap.get(cv2.CAP_PROP_FPS)
        if local_fps <= 0:
            raise ValueError(f"Invalid FPS value ({local_fps}) for video: {video_path}")

        local_total_frames = (
            total_frames if total_frames is not None else int(local_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        )
        frame_index = timestamp_to_frame_index(frame_time, local_fps)
        if frame_index < 0 or frame_index >= local_total_frames:
            return False

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        local_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = local_cap.read()
        if not ok:
            return False

        encoded = cv2.imencode(".jpg", frame)[1]
        encoded.tofile(output_path)
        return True
    finally:
        if owns_capture:
            local_cap.release()
