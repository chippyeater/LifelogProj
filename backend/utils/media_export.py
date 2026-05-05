import json
import os
import subprocess
from typing import Optional, Tuple

from utils.frame_utils import parse_timestamp_to_seconds


def normalize_base_url(base_url: str) -> str:
    return (base_url or "").rstrip("/")


def resolve_export_base_url(explicit_base_url: Optional[str] = None) -> Optional[str]:
    if explicit_base_url:
        return normalize_base_url(explicit_base_url)
    env_base_url = os.getenv("API_BASE_URL", "").strip()
    if env_base_url:
        return normalize_base_url(env_base_url)
    return None


def build_asset_url(base_url: Optional[str], user_id: Optional[str], relative_path: Optional[str]) -> str:
    if not base_url or not user_id or not relative_path:
        return ""
    clean_base = normalize_base_url(base_url)
    clean_path = str(relative_path).replace("\\", "/").lstrip("/")
    return f"{clean_base}/assets/{user_id}/{clean_path}"


def build_asset_relative_path(user_id: Optional[str], relative_path: Optional[str]) -> str:
    if not relative_path:
        return ""
    clean_path = str(relative_path).replace("\\", "/").lstrip("/")
    return clean_path


def format_seconds_to_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    secs = (total_ms % 60_000) // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def clamp_media_range(
    start_time: str,
    end_time: str,
    *,
    min_seconds: float,
    max_seconds: float,
) -> Tuple[str, str]:
    start_seconds = parse_timestamp_to_seconds(start_time)
    end_seconds = parse_timestamp_to_seconds(end_time)
    if end_seconds <= start_seconds:
        raise ValueError("end_time must be later than start_time")

    current_duration = end_seconds - start_seconds
    if current_duration > max_seconds:
        center = (start_seconds + end_seconds) / 2
        half = max_seconds / 2
        start_seconds = max(0.0, center - half)
        end_seconds = start_seconds + max_seconds
    elif current_duration < min_seconds:
        center = (start_seconds + end_seconds) / 2
        half = min_seconds / 2
        start_seconds = max(0.0, center - half)
        end_seconds = start_seconds + min_seconds

    return format_seconds_to_timestamp(start_seconds), format_seconds_to_timestamp(end_seconds)


def _run_ffmpeg(args: list[str]) -> None:
    ffmpeg = os.getenv("FFMPEG_BIN") or "ffmpeg"
    subprocess.run([ffmpeg, *args], check=True, capture_output=True, text=True)


def _probe_video_clip(path: str) -> dict:
    ffprobe = os.getenv("FFPROBE_BIN") or "ffprobe"
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    return {
        "video": video_stream,
        "audio": audio_stream,
    }


def _validate_exported_video_clip(path: str) -> None:
    stream_info = _probe_video_clip(path)
    video_stream = stream_info.get("video")
    if not video_stream:
        raise RuntimeError(f"exported video has no video stream: {path}")

    codec_name = (video_stream.get("codec_name") or "").lower()
    pix_fmt = (video_stream.get("pix_fmt") or "").lower()
    profile = (video_stream.get("profile") or "").lower()
    if codec_name != "h264":
        raise RuntimeError(f"exported video codec is not h264: codec={codec_name or 'unknown'} path={path}")
    if pix_fmt != "yuv420p":
        raise RuntimeError(f"exported video pixel format is not yuv420p: pix_fmt={pix_fmt or 'unknown'} path={path}")
    if "10" in profile:
        raise RuntimeError(f"exported video profile is not widely compatible: profile={profile} path={path}")


def export_audio_clip(video_path: str, start_time: str, end_time: str, output_path: str) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _run_ffmpeg(
        [
            "-y",
            "-i",
            video_path,
            "-ss",
            start_time,
            "-to",
            end_time,
            "-vn",
            "-acodec",
            "libmp3lame",
            "-ar",
            "44100",
            "-ac",
            "2",
            output_path,
        ]
    )
    return output_path


def export_video_clip(video_path: str, start_time: str, end_time: str, output_path: str) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _run_ffmpeg(
        [
            "-y",
            "-i",
            video_path,
            "-ss",
            start_time,
            "-to",
            end_time,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level:v",
            "4.0",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            output_path,
        ]
    )
    _validate_exported_video_clip(output_path)
    return output_path
