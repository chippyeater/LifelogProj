import argparse
import os
import json
import sys
import datetime
import subprocess
import shutil
import cv2
import logging
import re
from my_basics import ActivityContext
from pipeline_cache import PipelineCacheManager
from bailian_video_processor import create_bailian_processor
from make_context import (
    execute_entity_clue_enhancement,
    execute_scene_clue_enhancement,
    execute_video_processor_single_call,
    extract_reference_frames,
)
from clue_aigc_generator import VolcEngineAIGCGenerator
from generate_unity_json import generate_game_meta_flow, validate_generated_assets
from db import init_db, upsert_user_video, count_subevents, get_video_record, get_pipeline_state, set_pipeline_state
from runtime_config import get_config_value, resolve_backend_path
from utils.frame_utils import extract_frame_to_path

logger = logging.getLogger(__name__)

DEFAULT_ACTIVITY_TIME = str(get_config_value("pipeline.default_time", "2024年1月5日15:00"))
DEFAULT_LOCATION = str(get_config_value("pipeline.default_location", "超市"))
STAGE_NAME_BY_NUMBER = {
    1: "stage1_video_parse",
    2: "stage2_event_rebuild",
    3: "stage3_detail_generate",
}
TASK_STATUS_FILENAME = "status.json"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _status_path(output_root: str, user_id: str) -> str:
    return os.path.join(output_root, user_id, TASK_STATUS_FILENAME)


def _default_status_pipeline_state() -> dict:
    return {
        "events": "pending",
        "entities": "pending",
        "frames": "pending",
        "aigc": "pending",
        "unity": "pending",
        "last_error": None,
    }


def _read_status_doc(output_root: str, user_id: str) -> dict:
    path = _status_path(output_root, user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_status_doc(output_root: str, user_id: str, payload: dict) -> None:
    path = _status_path(output_root, user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _update_status_doc(
    *,
    output_root: str,
    user_id: str,
    video_name: str,
    task_id: str,
    status: str | None = None,
    ready: bool | None = None,
    progress: int | None = None,
    current_step: str | None = None,
    error: str | None = None,
    pipeline_state: dict | None = None,
) -> dict:
    doc = _read_status_doc(output_root, user_id)
    merged_pipeline_state = _default_status_pipeline_state()
    merged_pipeline_state.update(doc.get("pipeline_state") or {})
    merged_pipeline_state.update(pipeline_state or {})

    payload = {
        "ok": True,
        "user": user_id,
        "task_id": doc.get("task_id") or task_id,
        "job_id": doc.get("job_id") or task_id,
        "video": doc.get("video") or video_name,
        "status": status or doc.get("status") or "processing",
        "ready": ready if ready is not None else bool(doc.get("ready", False)),
        "progress": progress if progress is not None else int(doc.get("progress") or 0),
        "current_step": current_step or doc.get("current_step") or "等待处理",
        "error": error if error is not None else doc.get("error"),
        "pipeline_state": merged_pipeline_state,
        "updated_at": _utc_now_iso(),
    }
    _write_status_doc(output_root, user_id, payload)
    return payload


def _merge_pipeline_state(
    db_path: str,
    user_id: str,
    video_name: str,
    output_root: str | None = None,
    task_id: str | None = None,
    **updates,
) -> dict:
    # 增量更新 pipeline_state，避免每次调用都覆盖其他阶段状态
    state = get_pipeline_state(db_path, user_id, video_name)
    state.update({k: v for k, v in updates.items() if v is not None})
    set_pipeline_state(db_path, user_id, video_name, state)
    if output_root and task_id:
        _update_status_doc(
            output_root=output_root,
            user_id=user_id,
            video_name=video_name,
            task_id=task_id,
            pipeline_state=state,
        )
    return state


def _mark_pipeline_failure(
    db_path: str,
    user_id: str,
    video_name: str,
    stage_key: str | None,
    exc: Exception,
    output_root: str | None = None,
    task_id: str | None = None,
) -> dict:
    # 统一记录失败阶段与错误信息，供 API/Unity 查询
    updates = {"last_error": str(exc) or exc.__class__.__name__}
    if stage_key:
        updates[stage_key] = "failed"
    state = _merge_pipeline_state(
        db_path,
        user_id,
        video_name,
        output_root=output_root,
        task_id=task_id,
        **updates,
    )
    upsert_user_video(
        db_path,
        user_id=user_id,
        video_name=video_name,
        fields={"status": "failed"},
    )
    if output_root and task_id:
        _update_status_doc(
            output_root=output_root,
            user_id=user_id,
            video_name=video_name,
            task_id=task_id,
            status="failed",
            ready=False,
            error=updates["last_error"],
            pipeline_state=state,
            current_step="处理失败",
        )
    return state


def _should_stop_after_stage(current_stage: int, end_stage: int | None) -> bool:
    # 当前阶段完成后，判断是否需要提前停止流水线
    return end_stage is not None and current_stage >= end_stage


def _validate_pipeline_mode_args(pipeline_mode: str, start_stage: int, end_stage: int | None) -> None:
    # full_context 模式只支持从头一口气跑完整旧链路，不和分阶段控制混用
    if pipeline_mode == "full_context":
        if start_stage != 1:
            raise ValueError("full_context 模式不支持 --start-stage")
        if end_stage is not None:
            raise ValueError("full_context 模式不支持 --end-stage")


def _backfill_stage1_image_paths(stage1_output: dict, video_path: str, frames_dir: str, user_dir: str) -> None:
    # 根据阶段1中的 frame_time 自动截帧，并回填 image_path
    timeline = stage1_output.get("timeline") or []
    for segment in timeline:
        if not isinstance(segment, dict):
            continue
        for keyframe in segment.get("keyframes") or []:
            if not isinstance(keyframe, dict):
                continue
            frame_id = keyframe.get("frame_id")
            frame_time = keyframe.get("frame_time")
            if not frame_id or not frame_time:
                keyframe["image_path"] = None
                continue
            image_path = os.path.join(frames_dir, f"{frame_id}.jpg")
            ok = extract_frame_to_path(video_path, frame_time, image_path)
            keyframe["image_path"] = os.path.relpath(image_path, user_dir).replace("\\", "/") if ok else None

def _format_duration(seconds: float) -> str:
        if seconds < 0:
            seconds = 0
        whole = int(seconds)
        hh = whole // 3600
        mm = (whole % 3600) // 60
        ss = whole % 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}"

# 计算视频时长
def _get_video_length(video_file: str) -> str | None:
    if not os.path.exists(video_file):
        return None
    cap = cv2.VideoCapture(video_file)
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    if not fps or fps <= 0 or not total_frames:
        return None
    return _format_duration(total_frames / fps)

def _clear_image_paths(full_context: dict) -> None:
    # 强制清空图片相关路径，便于重新抽帧与重生成
    if full_context.get("activity_visual_clue"):
        for clue in full_context.get("activity_visual_clue") or []:
            if isinstance(clue, dict):
                clue["reference_frame_path"] = None
                clue["enhanced_image_path"] = None
    for _event in (full_context.get("events") or {}).values():
        for clue in _event.get("scene_clues") or []:
            if isinstance(clue, dict):
                clue["reference_frame_path"] = None
                clue["enhanced_image_path"] = None
        for group in ("key_persons", "key_objects"):
            for entity in _event.get(group, []) or []:
                if isinstance(entity, dict):
                    entity["reference_frame_path"] = None
                    entity["enhanced_image_path"] = None
    logger.info("Cleared existing image paths in context for all clues and entities.")

def _parse_iso6709_location(value: str) -> tuple[float, float] | None:
    if not value:
        return None
    s = value.strip()
    m = re.search(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        lat = float(m.group(1))
        lon = float(m.group(2))
        return lat, lon
    except ValueError:
        return None

def _parse_lat_lon(value: str) -> tuple[float, float] | None:
    if not value:
        return None
    s = value.strip()
    m = re.search(r"lat(?:itude)?\s*=?\s*([+-]?\d+(?:\.\d+)?)", s, flags=re.IGNORECASE)
    n = re.search(r"lon(?:gitude)?\s*=?\s*([+-]?\d+(?:\.\d+)?)", s, flags=re.IGNORECASE)
    if m and n:
        try:
            return float(m.group(1)), float(n.group(1))
        except ValueError:
            return None
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None

def _parse_datetime(value: str) -> datetime.datetime | None:
    if not value:
        return None
    s = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(s.replace("Z", "+00:00"), fmt)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None

def _ffprobe_metadata(video_path: str) -> dict:
    ffprobe = str(get_config_value("binaries.ffprobe_bin", "ffprobe"))
    if not ffprobe or not os.path.exists(video_path):
        return {}
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format_tags:stream_tags",
        "-of",
        "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except Exception:
        return {}
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}

def _detect_capture_info(video_path: str) -> tuple[str | None, str | None, dict]:
    meta = _ffprobe_metadata(video_path)
    tags: dict = {}
    fmt_tags = (meta.get("format") or {}).get("tags") or {}
    tags.update(fmt_tags)
    for stream in meta.get("streams") or []:
        stream_tags = stream.get("tags") or {}
        tags.update(stream_tags)

    time_candidates = [
        tags.get("creation_time"),
        tags.get("com.apple.quicktime.creationdate"),
        tags.get("date"),
        tags.get("DATE"),
    ]
    capture_dt = None
    for cand in time_candidates:
        capture_dt = _parse_datetime(cand) if cand else None
        if capture_dt:
            break

    location_candidates = [
        tags.get("location"),
        tags.get("com.apple.quicktime.location.ISO6709"),
        tags.get("com.android.location"),
        tags.get("location-eng"),
    ]
    lat_lon = None
    for cand in location_candidates:
        lat_lon = _parse_iso6709_location(cand) or _parse_lat_lon(cand)
        if lat_lon:
            break

    capture_time_str = None
    if capture_dt:
        if capture_dt.tzinfo:
            capture_dt = capture_dt.astimezone()
        capture_time_str = capture_dt.strftime("%Y年%m月%d日%H:%M")

    location_str = None
    if lat_lon:
        location_str = f"GPS({lat_lon[0]:.6f}, {lat_lon[1]:.6f})"

    return capture_time_str, location_str, tags


def _load_json_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_stage_resume_files(cache_manager: PipelineCacheManager, start_stage: int) -> dict:
    if start_stage <= 1:
        return {}
    if start_stage == 2:
        return {
            "context_path": cache_manager.stage_file("stage1_video_parse", "stage1_timeline_output.json"),
        }
    if start_stage == 3:
        return {
            "context_path": cache_manager.stage_file("stage2_event_rebuild", "stage2_event_rebuild_output.json"),
        }
    raise ValueError(f"Unsupported start_stage: {start_stage}")


def _validate_resume_files(paths: dict) -> None:
    for label, path in paths.items():
        if not os.path.exists(path):
            raise FileNotFoundError(f"Resume file not found for {label}: {path}")


def main() -> None:
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    for name in ("huggingface_hub", "transformers", "sentence_transformers"):
        logging.getLogger(name).setLevel(logging.ERROR)
    pipeline_cfg = get_config_value("pipeline", {})
    compress_cfg = get_config_value("video_processing.compression", {})

    # 解析命令行参数
    parser = argparse.ArgumentParser(description="Run full pipeline for a user.")
    parser.add_argument("--user", required=True, help="User name (output subfolder)")
    parser.add_argument("--video", required=True, help="Video file name (will be prefixed with videos/)")
    parser.add_argument("--activity", default=pipeline_cfg.get("default_activity", "逛超市"), help="Activity description")
    parser.add_argument("--people", default=pipeline_cfg.get("default_people", "我"), help="People involved")
    parser.add_argument("--time", dest="activity_time", default=DEFAULT_ACTIVITY_TIME, help="Activity time")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help="Activity location")
    parser.add_argument(
        "--force-regenerate-images",
        action="store_true",
        help="Clear reference_frame_path/enhanced_image_path before regenerating images",
    )
    parser.add_argument("--output-root", default=pipeline_cfg.get("output_root", "output"), help="Root output directory")
    parser.add_argument("--base-url", default=None, help="Base URL for asset links (optional)")
    parser.add_argument(
        "--compress-video",
        action=argparse.BooleanOptionalAction,
        default=bool(compress_cfg.get("enabled_by_default", False)),
        help="Compress video before processing (default: False)",
    )
    parser.add_argument(
        "--pipeline-mode",
        choices=("staged", "full_context"),
        default=pipeline_cfg.get("pipeline_mode", "staged"),
        help="Pipeline mode: staged uses 3-stage cache chain, full_context uses the legacy one-shot prompt",
    )
    parser.add_argument("--compress-width", type=int, default=int(compress_cfg.get("scale_width", -2)), help="Compress width")
    parser.add_argument("--compress-height", type=int, default=int(compress_cfg.get("scale_height", 720)), help="Compress height")
    parser.add_argument(
        "--start-stage",
        type=int,
        choices=(1, 2, 3),
        default=int(pipeline_cfg.get("start_stage", 1)),
        help="Start pipeline from a cached stage (default: 1)",
    )
    parser.add_argument(
        "--end-stage",
        type=int,
        choices=(1, 2, 3),
        default=pipeline_cfg.get("end_stage"),
        help="Stop pipeline after the specified stage (currently supports 1, 2 or 3)",
    )
    parser.add_argument(
        "--confusion-count",
        type=int,
        default=int(pipeline_cfg.get("confusion_count", 2)),
        help="Number of confusion events to insert into narrative recall tasks (default: 0)",
    )
    args = parser.parse_args()
    if args.end_stage is not None and args.end_stage < args.start_stage:
        parser.error("--end-stage cannot be earlier than --start-stage")
    try:
        _validate_pipeline_mode_args(args.pipeline_mode, args.start_stage, args.end_stage)
    except ValueError as e:
        parser.error(str(e))

    # 创建用户输出目录
    user_dir = os.path.join(args.output_root, args.user)
    os.makedirs(user_dir, exist_ok=True)
    cache_manager = PipelineCacheManager(user_dir)
    cache_manager.ensure_dirs()

    # 记录本次运行的控制台输出（stdout/stderr）
    log_dir = os.path.join(user_dir, str(get_config_value("paths.logs_dirname", "logs")))
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"run_{ts}.log")

    class _Tee:
        def __init__(self, *streams):
            self._streams = streams

        def write(self, data):
            for s in self._streams:
                s.write(data)

        def flush(self):
            for s in self._streams:
                s.flush()

        def isatty(self):
            try:
                return any(getattr(s, "isatty", lambda: False)() for s in self._streams)
            except Exception:
                return False

    log_file = open(log_path, "a", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    # Reset logging handlers after tee so logs go to file as well
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    root_logger.addHandler(handler)
    logger.info("[log] console output is being saved to: %s", log_path)

    # 初始化/连接数据库
    db_path = resolve_backend_path(get_config_value("server.database_path", "lifelog.db"))
    init_db(db_path)

    # 解析视频路径与数据库记录
    video_name = os.path.basename(args.video)
    status_doc = _read_status_doc(args.output_root, args.user)
    task_id = status_doc.get("task_id") or os.path.splitext(video_name)[0]
    videos_rel_dir = str(get_config_value("paths.videos_dir", "videos"))
    source_video_path = os.path.join(videos_rel_dir, video_name)
    cache_manifest = cache_manager.build_manifest(
        user_id=args.user,
        video_name=video_name,
        source_video=source_video_path,
    )
    cache_manager.save_manifest(cache_manifest)
    logger.info("Pipeline mode: %s", args.pipeline_mode)
    if args.pipeline_mode == "staged":
        logger.info("Pipeline start stage: %s (%s)", args.start_stage, STAGE_NAME_BY_NUMBER[args.start_stage])
    current_stage_key = "events"

    def update_status(
        *,
        status: str | None = None,
        ready: bool | None = None,
        progress: int | None = None,
        current_step: str | None = None,
        error: str | None = None,
        pipeline_state: dict | None = None,
    ) -> dict:
        return _update_status_doc(
            output_root=args.output_root,
            user_id=args.user,
            video_name=video_name,
            task_id=task_id,
            status=status,
            ready=ready,
            progress=progress,
            current_step=current_step,
            error=error,
            pipeline_state=pipeline_state,
        )

    def _compress_video(input_path: str, output_path: str, width: int, height: int) -> None:
        # Config-managed: ffmpeg path and compression parameters come from runtime_config.json.
        compression_cfg = get_config_value("video_processing.compression", {})
        ffmpeg = str(get_config_value("binaries.ffmpeg_bin", "ffmpeg"))
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        cmd = [
            ffmpeg,
            "-y" if compression_cfg.get("overwrite", True) else "-n",
            "-i",
            input_path,
            "-vf",
            f"scale={int(width)}:{int(height)}",
            "-r",
            str(compression_cfg.get("frame_rate", 15)),
            "-c:v",
            str(compression_cfg.get("video_codec", "libx264")),
            "-b:v",
            str(compression_cfg.get("video_bitrate", "2M")),
            "-pix_fmt",
            str(compression_cfg.get("video_pixel_format", "yuv420p")),
        ]
        video_profile = str(compression_cfg.get("video_profile", "") or "").strip()
        if video_profile:
            cmd.extend(["-profile:v", video_profile])
        video_level = str(compression_cfg.get("video_level", "") or "").strip()
        if video_level:
            cmd.extend(["-level:v", video_level])
        preset = str(compression_cfg.get("preset", "") or "").strip()
        if preset:
            cmd.extend(["-preset", preset])
        if compression_cfg.get("keep_audio", True):
            cmd.extend(
                [
                    "-c:a",
                    str(compression_cfg.get("audio_codec", "aac")),
                    "-b:a",
                    str(compression_cfg.get("audio_bitrate", "128k")),
                ]
            )
        else:
            cmd.append("-an")
        movflags = str(compression_cfg.get("movflags", "") or "").strip()
        if movflags:
            cmd.extend(["-movflags", movflags])
        cmd.append(
            output_path,
        )
        logger.info("Running ffmpeg compression command: %s", subprocess.list2cmdline(cmd))
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if result.stdout and result.stdout.strip():
            logger.info("ffmpeg stdout:\n%s", result.stdout.strip())
        if result.stderr and result.stderr.strip():
            logger.info("ffmpeg stderr:\n%s", result.stderr.strip())

    try:
        if args.compress_video:
            # 按配置压缩后再进入后续处理
            compressed_dir = os.path.join(
                user_dir,
                str(get_config_value("video_processing.compression.output_subdir", "compressed")),
            )
            compressed_video_path = os.path.join(
                compressed_dir,
                (
                    f"{get_config_value('video_processing.compression.filename_prefix', 'compressed')}_"
                    f"{args.compress_width}x{args.compress_height}_{video_name}"
                ),
            )
            if os.path.exists(compressed_video_path):
                logger.info("Compressed video already exists: %s", compressed_video_path)
            else:
                _compress_video(source_video_path, compressed_video_path, args.compress_width, args.compress_height)
            video_path = compressed_video_path
        else:
            # 不压缩视频文件
            video_path = source_video_path
        
        record = get_video_record(db_path, args.user, video_name)
        video_url = record.get("video_url") if record else None
        _merge_pipeline_state(
            db_path,
            args.user,
            video_name,
            output_root=args.output_root,
            task_id=task_id,
            events="pending",
            entities="pending",
            frames="pending",
            aigc="pending",
            unity="pending",
            last_error=None,
        )
        update_status(
            status="processing",
            ready=False,
            progress=5,
            current_step="正在启动处理任务",
        )

        # 执行分析或加载缓存
        extracted_context_path = (record.get("extracted_context_path") if record else None) or os.path.join(
            user_dir, "extracted_context.json"
        )
        stage1_timeline_path = None
        stage1_output = None
        stage2_output = None
        stage3_output = None
        full_context = None
        ctx = None

        resume_files = {}
        if args.pipeline_mode == "staged":
            resume_files = _resolve_stage_resume_files(cache_manager, args.start_stage)

        if args.pipeline_mode == "staged" and args.start_stage == 2:
            _validate_resume_files(resume_files)
            stage1_output = _load_json_file(resume_files["context_path"])
            logger.info("Loaded cached stage1 output: %s", resume_files["context_path"])
        elif args.pipeline_mode == "staged" and args.start_stage == 3:
            _validate_resume_files(resume_files)
            stage2_output = _load_json_file(resume_files["context_path"])
            logger.info("Loaded cached stage2 output: %s", resume_files["context_path"])
        elif args.pipeline_mode == "full_context" and os.path.exists(extracted_context_path):
            full_context = _load_json_file(extracted_context_path)
            logger.info("Loaded cached full_context: %s", extracted_context_path)

        if full_context is None and (args.pipeline_mode != "staged" or args.start_stage == 1):
            detected_time, detected_location, _ = _detect_capture_info(video_path)
            if args.activity_time == DEFAULT_ACTIVITY_TIME and detected_time:
                logger.info("Detected capture time from metadata: %s", detected_time)
                args.activity_time = detected_time
            elif args.activity_time == DEFAULT_ACTIVITY_TIME:
                try:
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(video_path))
                    args.activity_time = mtime.strftime("%Y年%m月%d日%H:%M")
                    logger.info("No capture time metadata found; fallback to file mtime: %s", args.activity_time)
                except Exception:
                    logger.info("No capture time metadata found; using default time: %s", args.activity_time)

            if args.location == DEFAULT_LOCATION and detected_location:
                logger.info("Detected capture location from metadata: %s", detected_location)
                args.location = detected_location
            elif args.location == DEFAULT_LOCATION:
                logger.info("No capture location metadata found; using default location: %s", args.location)

            logger.info("Bailian processing for video: %s", video_url or video_path)
            video_processor = create_bailian_processor(
                video_url=video_url,
                video_path=video_path if video_url is None else None,
                output_dir=user_dir,
            )
            if not video_url and getattr(video_processor, "video_url", None):
                video_url = video_processor.video_url
                upsert_user_video(
                    db_path,
                    user_id=args.user,
                    video_name=video_name,
                    fields={
                        "user_id": args.user,
                        "video_name": video_name,
                        "video_path": source_video_path,
                        "video_url": video_url,
                        "status": "video_uploaded",
                    },
                )

            video_length = _get_video_length(video_path)
            ctx = ActivityContext(
                activity=args.activity,
                people=args.people,
                time=args.activity_time,
                location=args.location,
                video_length=video_length,
            )
            update_status(
                status="processing",
                ready=False,
                progress=10,
                current_step="开始提取 full_context",
            )

            if args.pipeline_mode == "full_context":
                full_context = video_processor.analyze_full_context(ctx)
                _merge_pipeline_state(
                    db_path,
                    args.user,
                    video_name,
                    output_root=args.output_root,
                    task_id=task_id,
                    events="done",
                    entities="done",
                )
                update_status(
                    status="processing",
                    ready=False,
                    progress=40,
                    current_step="full_context 提取完成",
                )
            else:
                # staged 模式先落结构化阶段缓存，供单阶段调试和恢复
                stage1_output = video_processor.analyze_stage1_video_parse(ctx)
                stage1_output["metadata"]["source_video"] = source_video_path.replace("\\", "/")
                stage1_frames_dir = os.path.join(cache_manager.stage_dir("stage1_video_parse"), "frames")
                _backfill_stage1_image_paths(stage1_output, source_video_path, stage1_frames_dir, user_dir)
                stage1_timeline_path = cache_manager.write_json(
                    "stage1_video_parse",
                    "stage1_timeline_output.json",
                    stage1_output,
                )
                cache_manager.mark_stage(
                    cache_manifest,
                    "stage1_video_parse",
                    status="done",
                    files=[stage1_timeline_path],
                )
                cache_manager.save_manifest(cache_manifest)
                if _should_stop_after_stage(1, args.end_stage):
                    logger.info("Pipeline stopped after stage 1 as requested.")
                    return

        if args.pipeline_mode == "staged" and args.start_stage <= 2:
            current_stage_key = "events"
            if stage1_output is None:
                stage1_output = _load_json_file(cache_manager.stage_file("stage1_video_parse", "stage1_timeline_output.json"))
            if "video_processor" not in locals():
                video_processor = create_bailian_processor(
                    video_url=video_url,
                    video_path=video_path if video_url is None else None,
                    output_dir=user_dir,
                )
            stage2_output = video_processor.analyze_stage2_event_rebuild(stage1_output)
            stage2_path = cache_manager.write_json(
                "stage2_event_rebuild",
                "stage2_event_rebuild_output.json",
                stage2_output,
            )
            cache_manager.mark_stage(
                cache_manifest,
                "stage2_event_rebuild",
                status="done",
                files=[stage2_path],
            )
            cache_manager.save_manifest(cache_manifest)
            _merge_pipeline_state(
                db_path,
                args.user,
                video_name,
                output_root=args.output_root,
                task_id=task_id,
                events="done",
            )
            if _should_stop_after_stage(2, args.end_stage):
                logger.info("Pipeline stopped after stage 2 as requested.")
                return

        if args.pipeline_mode == "staged" and args.start_stage <= 3:
            current_stage_key = "entities"
            if stage1_output is None:
                stage1_output = _load_json_file(cache_manager.stage_file("stage1_video_parse", "stage1_timeline_output.json"))
            if stage2_output is None:
                stage2_output = _load_json_file(cache_manager.stage_file("stage2_event_rebuild", "stage2_event_rebuild_output.json"))
            if "video_processor" not in locals():
                video_processor = create_bailian_processor(
                    video_url=video_url,
                    video_path=video_path if video_url is None else None,
                    output_dir=user_dir,
                )
            stage3_output = video_processor.analyze_stage3_detail_generate(stage1_output, stage2_output)
            stage3_structured_path = cache_manager.write_json(
                "stage3_detail_generate",
                "stage3_detail_generate_output.json",
                stage3_output,
            )
            cache_manager.mark_stage(
                cache_manifest,
                "stage3_detail_generate",
                status="done",
                files=[stage3_structured_path],
            )
            cache_manager.save_manifest(cache_manifest)
            _merge_pipeline_state(
                db_path,
                args.user,
                video_name,
                output_root=args.output_root,
                task_id=task_id,
                entities="done",
            )
            if _should_stop_after_stage(3, args.end_stage):
                logger.info("Pipeline stopped after stage 3 as requested.")
                return

        if full_context is None and args.pipeline_mode == "staged":
            current_stage_key = "entities"
            if ctx is None and stage1_output is not None:
                stage1_metadata = stage1_output.get("metadata") or {}
                ctx = ActivityContext(
                    activity=stage1_metadata.get("activity_name", args.activity),
                    people=args.people,
                    time=stage1_metadata.get("time_info", args.activity_time),
                    location=stage1_metadata.get("location_info", args.location),
                    video_length=_get_video_length(video_path),
                )
            # staged 模式当前仍通过旧 full_context 桥接到 Unity 导出链路
            full_context = execute_video_processor_single_call(
                ctx,
                video_processor,
                checkpoint_path=extracted_context_path,
            )
            _merge_pipeline_state(
                db_path,
                args.user,
                video_name,
                output_root=args.output_root,
                task_id=task_id,
                events="done",
                entities="done",
            )

        if not full_context:
            raise RuntimeError("No context extracted")

        # 统一记录源视频路径，供后续 flow 中的音频/视频线索引用
        full_context["source_video"] = source_video_path.replace("\\", "/")

        if args.pipeline_mode == "staged":
            stage1_legacy_path = cache_manager.write_json(
                "stage1_video_parse",
                "video_parse_output.json",
                full_context,
            )
            stage1_files = [stage1_legacy_path]
            if stage1_timeline_path:
                stage1_files.insert(0, stage1_timeline_path)
            cache_manager.mark_stage(
                cache_manifest,
                "stage1_video_parse",
                status="done",
                files=stage1_files,
            )
            cache_manager.save_manifest(cache_manifest)

        current_stage_key = "frames"
        if args.pipeline_mode == "full_context" or args.start_stage <= 2:
            # 旧链路的抽帧/AIGC 仍然直接消费 full_context
            update_status(
                status="processing",
                ready=False,
                progress=50,
                current_step="正在抽取参考帧",
            )
            if args.force_regenerate_images:
                _clear_image_paths(full_context)
            frames_dir = os.path.join(user_dir, "frames")
            extract_reference_frames(full_context, source_video_path, output_dir=frames_dir)
            _merge_pipeline_state(
                db_path,
                args.user,
                video_name,
                output_root=args.output_root,
                task_id=task_id,
                frames="done",
            )
            update_status(
                status="processing",
                ready=False,
                progress=60,
                current_step="参考帧抽取完成",
            )

            current_stage_key = "aigc"
            update_status(
                status="processing",
                ready=False,
                progress=65,
                current_step="开始生成 AIGC 增强图",
            )
            volc_access_key = get_config_value("models.volcengine.access_key")
            volc_secret_key = get_config_value("models.volcengine.secret_key")
            aigc_state = "skipped"
            if volc_access_key and volc_secret_key:
                generator = VolcEngineAIGCGenerator(
                    access_key=volc_access_key,
                    secret_key=volc_secret_key,
                    output_dir=os.path.join(user_dir, "enhanced"),
                    add_logo=bool(get_config_value("aigc.add_logo", False)),
                    add_aigc_meta=bool(get_config_value("aigc.add_aigc_meta", True)),
                )
                execute_scene_clue_enhancement(
                    full_context,
                    generator,
                    checkpoint_path=extracted_context_path,
                    force=args.force_regenerate_images,
                )
                execute_entity_clue_enhancement(
                    full_context,
                    generator,
                    source_video_path,
                    checkpoint_path=extracted_context_path,
                    frames_dir=frames_dir,
                    force=args.force_regenerate_images,
                )
                aigc_state = "done"
            _merge_pipeline_state(
                db_path,
                args.user,
                video_name,
                output_root=args.output_root,
                task_id=task_id,
                aigc=aigc_state,
            )
            update_status(
                status="processing",
                ready=False,
                progress=75,
                current_step="AIGC 阶段完成" if aigc_state == "done" else "AIGC 已跳过",
            )

            with open(extracted_context_path, "w", encoding="utf-8") as f:
                json.dump(full_context, f, ensure_ascii=False, indent=2)

            if args.pipeline_mode == "staged":
                stage2_legacy_path = cache_manager.write_json(
                    "stage2_event_rebuild",
                    "event_rebuild_output.json",
                    full_context,
                )
                stage2_files = [stage2_legacy_path]
                stage2_structured_path = cache_manager.stage_file("stage2_event_rebuild", "stage2_event_rebuild_output.json")
                if os.path.exists(stage2_structured_path):
                    stage2_files.insert(0, stage2_structured_path)
                cache_manager.mark_stage(
                    cache_manifest,
                    "stage2_event_rebuild",
                    status="done",
                    files=stage2_files,
                )
                cache_manager.save_manifest(cache_manifest)
        else:
            with open(extracted_context_path, "w", encoding="utf-8") as f:
                json.dump(full_context, f, ensure_ascii=False, indent=2)

        subevent_count = count_subevents(extracted_context_path) if os.path.exists(extracted_context_path) else None
        upsert_user_video(
            db_path,
            user_id=args.user,
            video_name=video_name,
            fields={
                "user_id": args.user,
                "video_name": video_name,
                "video_path": source_video_path,
                "video_url": video_url,
                "status": "context_extracted",
                "extracted_context_path": extracted_context_path,
                "subevent_count": subevent_count,
            },
        )

        current_stage_key = "unity"
        if args.pipeline_mode == "full_context" or args.start_stage <= 3:
            # 生成Unity侧的GameMeta/GameFlow
            update_status(
                status="processing",
                ready=False,
                progress=85,
                current_step="开始生成 Unity JSON",
            )
            game_meta, game_flow = generate_game_meta_flow(
                input_path=extracted_context_path,
                output_dir=user_dir,
                confusion_count=args.confusion_count,
                base_url=args.base_url,
                user_id=args.user,
                regenerate_assets=args.force_regenerate_images,
            )
            asset_validation = validate_generated_assets(user_dir, game_meta, game_flow)
            if not asset_validation["ok"]:
                raise RuntimeError(
                    "Unity asset validation failed: "
                    f"missing={len(asset_validation['missing_assets'])}, "
                    f"non_ascii={len(asset_validation['non_ascii_assets'])}"
                )

            stage3_meta_path = cache_manager.write_json(
                "stage3_detail_generate",
                "GameMeta.json",
                game_meta,
            )
            stage3_flow_path = cache_manager.write_json(
                "stage3_detail_generate",
                "GameFlow.json",
                game_flow,
            )
            cache_manager.mark_stage(
                cache_manifest,
                "stage3_detail_generate",
                status="done",
                files=[p for p in [stage3_output and cache_manager.stage_file("stage3_detail_generate", "stage3_detail_generate_output.json"), stage3_meta_path, stage3_flow_path] if p],
            )
            cache_manager.save_manifest(cache_manifest)
            update_status(
                status="processing",
                ready=False,
                progress=95,
                current_step="资源校验完成",
            )
        else:
            game_meta = _load_json_file(cache_manager.stage_file("stage3_detail_generate", "GameMeta.json"))
            game_flow = _load_json_file(cache_manager.stage_file("stage3_detail_generate", "GameFlow.json"))
            _merge_pipeline_state(
                db_path,
                args.user,
                video_name,
                output_root=args.output_root,
                task_id=task_id,
                unity="done",
            )

        # 更新数据库：游戏数据已就绪
        upsert_user_video(
            db_path,
            user_id=args.user,
            video_name=video_name,
            fields={
                "user_id": args.user,
                "video_name": video_name,
                "status": "all_ready",
                "gameflow_path": os.path.join(user_dir, "GameFlow.json"),
                "gamemeta_path": os.path.join(user_dir, "GameMeta.json"),
                "processed_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            },
        )
        _merge_pipeline_state(
            db_path,
            args.user,
            video_name,
            output_root=args.output_root,
            task_id=task_id,
            unity="done",
        )
        update_status(
            status="all_ready",
            ready=True,
            progress=100,
            current_step="处理完成",
        )
    except Exception as exc:
        _mark_pipeline_failure(
            db_path,
            args.user,
            video_name,
            current_stage_key,
            exc,
            output_root=args.output_root,
            task_id=task_id,
        )
        logger.exception("Pipeline failed at stage=%s", current_stage_key)
        raise


if __name__ == "__main__":
    main()
