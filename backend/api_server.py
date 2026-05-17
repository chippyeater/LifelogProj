import json
import os
import asyncio
import hashlib
import time
import logging
import re
import shutil
import sys
import uuid
import subprocess
from typing import Tuple, Dict, Any, Optional

from flask import Flask, Response, jsonify, send_from_directory, request, abort
import mimetypes
from generate_unity_json import generate_game_meta_flow, validate_generated_assets
from db import (
    delete_user_task,
    get_pipeline_state,
    get_video_record,
    get_latest_video_record,
    list_users,
    upsert_user,
    upsert_user_video,
    init_db,
    set_pipeline_state,
)
from runtime_config import get_config_value, resolve_backend_path

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ROOT = resolve_backend_path(get_config_value("paths.output_root", "output"))
DB_PATH = resolve_backend_path(get_config_value("server.database_path", "lifelog.db"))
VIDEOS_REL_DIR = str(get_config_value("paths.videos_dir", "videos"))
VIDEOS_DIR = resolve_backend_path(VIDEOS_REL_DIR)
UPLOAD_CHUNKS_ROOT = resolve_backend_path(get_config_value("paths.upload_chunks_dir", "upload_chunks"))

app = Flask(__name__)
logger = logging.getLogger(__name__)
TASK_STATUS_FILENAME = "status.json"
RECALL_REPORT_FILENAME = "recall_report.json"
_DELETE_STATUS_MAP = {
    "idle": "pending",
    "not_started": "pending",
    "uploading": "processing",
    "processing": "processing",
    "completed": "all_ready",
    "all_ready": "all_ready",
    "error": "failed",
    "failed": "failed",
    "context_extracted": "context_extracted",
    "video_uploaded": "video_uploaded",
    "queued": "queued",
}
_DELETE_STATUS_BUCKETS = {
    "pending": "idle",
    "idle": "idle",
    "not_started": "idle",
    "queued": "processing",
    "video_uploaded": "processing",
    "processing": "processing",
    "context_extracted": "processing",
    "all_ready": "completed",
    "completed": "completed",
    "failed": "error",
    "error": "error",
}


@app.before_request
def _mark_request_start():
    request.start_time = time.perf_counter()


def _normalize_base_url(base_url: str) -> str:
    # 规范化基础 URL，移除末尾斜杠，避免重复拼接
    return base_url.rstrip("/")


def _get_base_url() -> str:
    # 获取对外可访问的基础 URL（优先环境变量，否则使用请求 host）
    configured_base = get_config_value("server.api_base_url", "")
    if configured_base:
        return _normalize_base_url(configured_base)
    return _normalize_base_url(request.host_url)


def _resolve_output_dir(user_id: str) -> str:
    # 解析用户输出目录，确保空 user 时使用默认目录
    safe_user = user_id.strip() or "default"
    return os.path.join(OUTPUT_ROOT, safe_user)


def _resolve_paths(user_id: str) -> Tuple[str, str, str]:
    # 解析 extracted_context / GameMeta / GameFlow 的文件路径
    out_dir = _resolve_output_dir(user_id)
    extracted = os.path.join(out_dir, "extracted_context.json")
    game_meta = os.path.join(out_dir, "GameMeta.json")
    game_flow = os.path.join(out_dir, "GameFlow.json")
    return extracted, game_meta, game_flow


def _resolve_status_path(user_id: str) -> str:
    return os.path.join(_resolve_output_dir(user_id), TASK_STATUS_FILENAME)


def _resolve_recall_report_path(user_id: str) -> str:
    return os.path.join(_resolve_output_dir(user_id), RECALL_REPORT_FILENAME)


def _normalize_delete_status(raw_status: str) -> str:
    return _DELETE_STATUS_MAP.get((raw_status or "").strip().lower(), (raw_status or "").strip().lower())


def _delete_status_bucket(raw_status: str) -> str:
    normalized = _normalize_delete_status(raw_status)
    return _DELETE_STATUS_BUCKETS.get(normalized, normalized)


def _remove_file_if_within(root_dir: str, target_path: str) -> None:
    if not target_path:
        return
    root_abs = os.path.abspath(root_dir)
    target_abs = os.path.abspath(target_path)
    if os.path.commonpath([root_abs, target_abs]) != root_abs:
        raise ValueError(f"refusing to delete path outside root: {target_path}")
    if os.path.isfile(target_abs):
        os.remove(target_abs)


def _remove_tree_if_within(root_dir: str, target_path: str) -> None:
    if not target_path:
        return
    root_abs = os.path.abspath(root_dir)
    target_abs = os.path.abspath(target_path)
    if os.path.commonpath([root_abs, target_abs]) != root_abs:
        raise ValueError(f"refusing to delete path outside root: {target_path}")
    if os.path.isdir(target_abs):
        shutil.rmtree(target_abs, ignore_errors=False)


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_name_component(value: Optional[str], fallback: str) -> str:
    text = (value or "").strip()
    if not text:
        text = fallback
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def _make_task_id(user_id: str) -> str:
    return f"task_{_safe_name_component(user_id, 'user')}_{uuid.uuid4().hex[:10]}"


def _build_current_step(pipeline_state: Dict[str, Any], status: str) -> str:
    if status == "failed":
        return "处理失败"
    if status == "all_ready":
        return "处理完成"
    if pipeline_state.get("unity") == "done":
        return "已生成 Unity 数据"
    if pipeline_state.get("aigc") == "done":
        return "正在生成 Unity 数据"
    if pipeline_state.get("frames") == "done":
        return "正在生成增强图"
    if pipeline_state.get("entities") == "done":
        return "正在抽取参考帧"
    if pipeline_state.get("events") == "done":
        return "正在分析视频细节"
    if status in ("video_uploaded", "processing", "queued"):
        return "正在分析视频"
    if status == "context_extracted":
        return "正在生成 Unity 数据"
    return "等待处理"


def _status_json_payload(
    *,
    user_id: str,
    task_id: str,
    job_id: str,
    video_name: str,
    status: str,
    ready: bool,
    progress: int,
    current_step: str,
    pipeline_state: Dict[str, Any],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "user": user_id,
        "task_id": task_id,
        "job_id": job_id,
        "video": video_name,
        "status": status,
        "ready": ready,
        "progress": progress,
        "current_step": current_step,
        "error": error,
        "pipeline_state": pipeline_state,
        "updated_at": _utc_now_iso(),
    }


def _read_status_json(user_id: str) -> Dict[str, Any]:
    path = _resolve_status_path(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_status_json(user_id: str, payload: Dict[str, Any]) -> None:
    out_dir = _resolve_output_dir(user_id)
    os.makedirs(out_dir, exist_ok=True)
    with open(_resolve_status_path(user_id), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _chunk_dir(identifier: str) -> str:
    safe_identifier = _safe_name_component(identifier, "upload")
    return os.path.join(UPLOAD_CHUNKS_ROOT, safe_identifier)


def _parse_positive_int(raw_value: Optional[str], field_name: str, *, allow_zero: bool = False) -> int:
    try:
        value = int((raw_value or "").strip())
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer")
    if allow_zero:
        if value < 0:
            raise ValueError(f"{field_name} must be >= 0")
    elif value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return value


def _build_video_name(original_name: str, user_id: str, task_id: str) -> str:
    original_name = os.path.basename((original_name or "").strip())
    stem, ext = os.path.splitext(original_name)
    ext = ext or ".mp4"
    safe_stem = _safe_name_component(stem, "upload")
    video_name = f"{_safe_name_component(user_id, 'user')}_{task_id}_{safe_stem}{ext}"
    return video_name


def _save_uploaded_video(file_storage, user_id: str, task_id: str) -> str:
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    video_name = _build_video_name(getattr(file_storage, "filename", ""), user_id, task_id)
    save_path = os.path.join(VIDEOS_DIR, video_name)
    file_storage.save(save_path)
    return video_name


def _save_chunk_upload(*, chunk_storage, identifier: str, filename: str, chunk_index: int, total_chunks: int) -> None:
    chunk_dir = _chunk_dir(identifier)
    os.makedirs(chunk_dir, exist_ok=True)

    metadata_path = os.path.join(chunk_dir, "meta.json")
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except Exception:
            metadata = {}
        recorded_total = metadata.get("totalChunks")
        recorded_filename = metadata.get("filename")
        if recorded_total is not None and int(recorded_total) != total_chunks:
            raise ValueError("totalChunks does not match existing upload session")
        if recorded_filename and recorded_filename != filename:
            raise ValueError("filename does not match existing upload session")

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "identifier": identifier,
                "filename": filename,
                "totalChunks": total_chunks,
                "updatedAt": _utc_now_iso(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    chunk_path = os.path.join(chunk_dir, str(chunk_index))
    chunk_storage.save(chunk_path)


def _cleanup_chunk_dir(identifier: str) -> None:
    chunk_dir = _chunk_dir(identifier)
    if os.path.isdir(chunk_dir):
        shutil.rmtree(chunk_dir, ignore_errors=True)


def _assemble_chunked_video(identifier: str, original_filename: str, user_id: str, task_id: str) -> str:
    chunk_dir = _chunk_dir(identifier)
    if not os.path.isdir(chunk_dir):
        raise FileNotFoundError("chunk upload session not found")

    metadata_path = os.path.join(chunk_dir, "meta.json")
    metadata = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except Exception:
            metadata = {}

    total_chunks = metadata.get("totalChunks")
    if total_chunks is None:
        raise ValueError("chunk metadata is missing totalChunks")
    total_chunks = _parse_positive_int(str(total_chunks), "totalChunks")

    resolved_filename = (original_filename or metadata.get("filename") or "").strip()
    if not resolved_filename:
        raise ValueError("videoFilename is required for chunk assembly")

    os.makedirs(VIDEOS_DIR, exist_ok=True)
    video_name = _build_video_name(resolved_filename, user_id, task_id)
    output_path = os.path.join(VIDEOS_DIR, video_name)

    try:
        with open(output_path, "wb") as output_file:
            for chunk_index in range(total_chunks):
                chunk_path = os.path.join(chunk_dir, str(chunk_index))
                if not os.path.exists(chunk_path):
                    raise FileNotFoundError(f"missing chunk {chunk_index}")
                with open(chunk_path, "rb") as chunk_file:
                    shutil.copyfileobj(chunk_file, output_file, length=1024 * 1024)
    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise

    _cleanup_chunk_dir(identifier)
    return video_name


def _launch_pipeline_process(
    *,
    user_id: str,
    video_name: str,
    activity: str,
    people: str,
    activity_time: str,
    location: str,
    confusion_count: Optional[int] = None,
    force_regenerate_images: bool = False,
) -> None:
    logger.info("Launching pipeline process for user=%s video=%s", user_id, video_name)
    cmd = [
        sys.executable,
        os.path.join(APP_DIR, "run_pipeline.py"),
        "--user",
        user_id,
        "--video",
        video_name,
        "--compress-video",
        "--pipeline-mode",
        "full_context",
        "--activity",
        activity,
        "--people",
        people,
        "--time",
        activity_time,
        "--location",
        location,
    ]
    if confusion_count is not None:
        cmd.extend(["--confusion-count", str(confusion_count)])
    if force_regenerate_images:
        cmd.append("--force-regenerate-images")
    subprocess.Popen(
        cmd,
        cwd=APP_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _build_api_url(base_url: str, path: str) -> str:
    # 统一拼接对外 URL，避免重复处理斜杠
    return f"{_normalize_base_url(base_url)}/{path.lstrip('/')}"


def _json_response(payload: Dict[str, Any], status_code: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        status=status_code,
        content_type="application/json; charset=utf-8",
    )


def _resolve_tts_dir(user_id: str) -> str:
    # 解析 TTS 缓存目录
    return os.path.join(_resolve_output_dir(user_id), "tts")


def _build_tts_cache_key(text: str, voice: str, rate: str, volume: str, fmt: str) -> str:
    # 基于请求参数生成稳定缓存 key
    payload = f"{voice}|{rate}|{volume}|{fmt}|{text}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


async def _edge_tts_save(text: str, voice: str, rate: str, volume: str, out_path: str) -> None:
    # 使用 edge-tts 生成语音并保存
    try:
        import edge_tts
    except Exception as exc:
        raise RuntimeError("edge-tts not installed") from exc

    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, volume=volume)
    await communicate.save(out_path)

def _validate_tts_file(path: str) -> Tuple[bool, str]:
    if not os.path.exists(path):
        return False, "file_not_found"
    size = os.path.getsize(path)
    if size <= 0:
        return False, "empty_file"
    return True, ""


def _should_regenerate(extracted_path: str, game_meta_path: str, game_flow_path: str) -> bool:
    # 判断是否需要重新生成 GameMeta/GameFlow（源数据更新或目标缺失）
    if not os.path.exists(extracted_path):
        return False
    if not os.path.exists(game_meta_path) or not os.path.exists(game_flow_path):
        return True
    try:
        src_time = os.path.getmtime(extracted_path)
        meta_time = os.path.getmtime(game_meta_path)
        flow_time = os.path.getmtime(game_flow_path)
    except OSError:
        return True
    return src_time > min(meta_time, flow_time)


def _ensure_generated(base_url: str, user_id: str, force: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    # 确保 GameMeta/GameFlow 已生成，必要时重新生成并返回结果
    extracted_path, game_meta_path, game_flow_path = _resolve_paths(user_id)
    output_dir = _resolve_output_dir(user_id)
    confusion_count = int(get_config_value("pipeline.confusion_count", 2))
    if force or _should_regenerate(extracted_path, game_meta_path, game_flow_path):
        return generate_game_meta_flow(
            input_path=extracted_path,
            output_dir=output_dir,
            confusion_count=confusion_count,
            base_url=base_url,
            user_id=user_id,
        )

    with open(game_meta_path, "r", encoding="utf-8") as f:
        game_meta = json.load(f)
    with open(game_flow_path, "r", encoding="utf-8") as f:
        game_flow = json.load(f)

    validation = validate_generated_assets(output_dir, game_meta, game_flow)
    if validation["ok"]:
        return game_meta, game_flow

    return generate_game_meta_flow(
        input_path=extracted_path,
        output_dir=output_dir,
        confusion_count=confusion_count,
        base_url=base_url,
        user_id=user_id,
        regenerate_assets=True,
    )


def _pipeline_progress_percent(pipeline_state: Dict[str, Any], status: str) -> int:
    # 用阶段完成情况粗略估算进度，供 Unity 轮询显示
    done_count = 0
    for key in ("events", "entities", "frames", "aigc", "unity"):
        if pipeline_state.get(key) == "done":
            done_count += 1

    if status == "all_ready":
        return 100
    if done_count > 0:
        return int(done_count / 5 * 100)
    if status == "video_uploaded":
        return 5
    if status == "context_extracted":
        return 60
    return 0


def _default_pipeline_state() -> Dict[str, Any]:
    return {
        "events": "pending",
        "entities": "pending",
        "frames": "pending",
        "aigc": "pending",
        "unity": "pending",
        "last_error": None,
    }


def _paths_record_for_user(user_id: str, video_name: str = "") -> Dict[str, Any]:
    extracted_path, game_meta_path, game_flow_path = _resolve_paths(user_id)
    return {
        "video_name": video_name,
        "status": "pending",
        "video_url": "",
        "extracted_context_path": extracted_path if os.path.exists(extracted_path) else "",
        "gamemeta_path": game_meta_path if os.path.exists(game_meta_path) else "",
        "gameflow_path": game_flow_path if os.path.exists(game_flow_path) else "",
        "subevent_count": None,
        "processed_at": None,
        "updated_at": None,
    }


def _build_asset_validation(user_id: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    if not rec.get("gamemeta_path") or not rec.get("gameflow_path"):
        return {"ok": False, "asset_count": 0, "missing_assets": [], "non_ascii_assets": []}

    try:
        with open(rec["gamemeta_path"], "r", encoding="utf-8") as f:
            game_meta = json.load(f)
        with open(rec["gameflow_path"], "r", encoding="utf-8") as f:
            game_flow = json.load(f)
    except Exception:
        return {"ok": False, "asset_count": 0, "missing_assets": [], "non_ascii_assets": []}

    return validate_generated_assets(_resolve_output_dir(user_id), game_meta, game_flow)


def _build_job_status_payload(base_url: str, user_id: str, video_name: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    pipeline_state = get_pipeline_state(DB_PATH, user_id, video_name)
    status = rec.get("status") or "pending"
    asset_validation = _build_asset_validation(user_id, rec)
    ready = status == "all_ready" and asset_validation["ok"]
    status_doc = _read_status_json(user_id)
    task_id = status_doc.get("task_id") or rec.get("task_id") or f"task_{_safe_name_component(user_id, 'user')}"
    current_step = status_doc.get("current_step") or _build_current_step(pipeline_state, status)
    progress = status_doc.get("progress")
    if not isinstance(progress, int):
        progress = _pipeline_progress_percent(pipeline_state, status)
    error = pipeline_state.get("last_error")
    if status_doc.get("error"):
        error = status_doc.get("error")

    if ready:
        current_step = "处理完成"
        progress = 100
        status = "all_ready"
    elif status == "failed" and not error:
        error = "pipeline failed"

    return {
        "ok": True,
        "user": user_id,
        "task_id": task_id,
        "job_id": status_doc.get("job_id") or task_id,
        "video": video_name,
        "status": status,
        "progress": progress,
        "current_step": current_step,
        "ready": ready,
        "pipeline_state": pipeline_state,
        "error": error,
        "asset_validation": asset_validation,
        "video_url": rec.get("video_url") or "",
        "extracted_context_url": _build_api_url(base_url, f"/api/context?user={user_id}") if rec.get("extracted_context_path") else "",
        "game_meta_url": _build_api_url(base_url, f"/api/game-meta?user={user_id}") if rec.get("gamemeta_path") else "",
        "game_flow_url": _build_api_url(base_url, f"/api/game-flow?user={user_id}") if rec.get("gameflow_path") else "",
        "subevent_count": rec.get("subevent_count"),
        "processed_at": rec.get("processed_at"),
        "updated_at": rec.get("updated_at"),
    }


def _build_job_status_from_status_doc(base_url: str, user_id: str, status_doc: Dict[str, Any]) -> Dict[str, Any]:
    video_name = status_doc.get("video") or ""
    pipeline_state = dict(_default_pipeline_state())
    pipeline_state.update(status_doc.get("pipeline_state") or {})

    status = status_doc.get("status") or "pending"
    rec = _paths_record_for_user(user_id, video_name)
    asset_validation = _build_asset_validation(user_id, rec)
    ready = bool(status_doc.get("ready")) and asset_validation["ok"]
    if status == "all_ready" and asset_validation["ok"]:
        ready = True

    error = status_doc.get("error") or pipeline_state.get("last_error")
    progress = status_doc.get("progress")
    if not isinstance(progress, int):
        progress = _pipeline_progress_percent(pipeline_state, status)
    current_step = status_doc.get("current_step") or _build_current_step(pipeline_state, status)

    if ready:
        status = "all_ready"
        progress = 100
        current_step = "处理完成"
    elif status == "failed" and not error:
        error = "pipeline failed"

    return {
        "ok": True,
        "user": user_id,
        "task_id": status_doc.get("task_id") or "",
        "job_id": status_doc.get("job_id") or status_doc.get("task_id") or "",
        "video": video_name,
        "status": status,
        "progress": progress,
        "current_step": current_step,
        "ready": ready,
        "pipeline_state": pipeline_state,
        "error": error,
        "asset_validation": asset_validation,
        "video_url": "",
        "extracted_context_url": _build_api_url(base_url, f"/api/context?user={user_id}") if rec.get("extracted_context_path") else "",
        "game_meta_url": _build_api_url(base_url, f"/api/game-meta?user={user_id}") if rec.get("gamemeta_path") else "",
        "game_flow_url": _build_api_url(base_url, f"/api/game-flow?user={user_id}") if rec.get("gameflow_path") else "",
        "subevent_count": None,
        "processed_at": None,
        "updated_at": status_doc.get("updated_at"),
    }


def _build_user_list_item(base_user: Dict[str, Any]) -> Dict[str, Any]:
    user_id = (base_user.get("id") or "").strip()
    user_name = (base_user.get("name") or user_id).strip() or user_id
    status_doc = _read_status_json(user_id)

    status = (base_user.get("status") or "pending").strip() or "pending"
    progress = 0
    updated_at = base_user.get("updated_at")

    if status_doc:
        status = (status_doc.get("status") or status).strip() or status
        updated_at = status_doc.get("updated_at") or updated_at
        progress = status_doc.get("progress")
        if not isinstance(progress, int):
            pipeline_state = dict(_default_pipeline_state())
            pipeline_state.update(status_doc.get("pipeline_state") or {})
            progress = _pipeline_progress_percent(pipeline_state, status)
    else:
        latest_video = get_latest_video_record(DB_PATH, user_id)
        if latest_video:
            status = (latest_video.get("status") or status).strip() or status
            updated_at = latest_video.get("updated_at") or updated_at
            video_name = latest_video.get("video_name") or ""
            if video_name:
                pipeline_state = get_pipeline_state(DB_PATH, user_id, video_name)
                progress = _pipeline_progress_percent(pipeline_state, status)

    if status == "all_ready":
        progress = 100
    if not isinstance(progress, int):
        progress = 0
    progress = max(0, min(100, progress))

    return {
        "id": user_id,
        "userId": user_id,
        "name": user_name,
        "userName": user_name,
        "status": status,
        "processingProgress": progress,
        "updated_at": updated_at,
    }


@app.post("/api/tasks/upload-chunk")
def upload_chunk():
    chunk_file = request.files.get("chunk")
    if chunk_file is None:
        return _json_response({"ok": False, "error": "chunk is required"}, 400)

    identifier = (request.form.get("identifier") or "").strip()
    filename = os.path.basename((request.form.get("filename") or "").strip())
    if not identifier:
        return _json_response({"ok": False, "error": "identifier is required"}, 400)
    if not filename:
        return _json_response({"ok": False, "error": "filename is required"}, 400)

    try:
        chunk_index = _parse_positive_int(request.form.get("chunkIndex"), "chunkIndex", allow_zero=True)
        total_chunks = _parse_positive_int(request.form.get("totalChunks"), "totalChunks")
        if chunk_index >= total_chunks:
            raise ValueError("chunkIndex must be less than totalChunks")
        _save_chunk_upload(
            chunk_storage=chunk_file,
            identifier=identifier,
            filename=filename,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )
    except ValueError as exc:
        return _json_response({"ok": False, "error": str(exc)}, 400)

    return _json_response(
        {
            "ok": True,
            "identifier": identifier,
            "filename": filename,
            "chunkIndex": chunk_index,
            "totalChunks": total_chunks,
        },
        201,
    )


@app.post("/api/tasks/process")
def create_process_task():
    user_id = (request.form.get("user") or "default").strip() or "default"
    user_name = (request.form.get("userName") or "").strip() or user_id
    uploaded_video = request.files.get("video")
    video_identifier = (request.form.get("videoIdentifier") or "").strip()
    video_filename = os.path.basename((request.form.get("videoFilename") or "").strip())
    if (uploaded_video is None or not getattr(uploaded_video, "filename", "")) and not video_identifier:
        return _json_response({"ok": False, "error": "video or videoIdentifier is required"}, 400)

    activity = (request.form.get("activity") or "逛超市").strip() or "逛超市"
    people = (request.form.get("people") or "我").strip() or "我"
    activity_time = (request.form.get("time") or "2024年1月5日15:00").strip() or "2024年1月5日15:00"
    location = (request.form.get("location") or "超市").strip() or "超市"
    shopping_list_raw = (request.form.get("shopping_list") or "").strip()
    confusion_count_raw = (request.form.get("confusion_event_count") or "").strip()
    confusion_count: Optional[int] = None
    if confusion_count_raw:
        try:
            confusion_count = _parse_positive_int(confusion_count_raw, "confusion_event_count", allow_zero=True)
        except ValueError as exc:
            return _json_response({"ok": False, "error": str(exc)}, 400)

    task_id = _make_task_id(user_id)
    job_id = task_id
    try:
        if uploaded_video is not None and getattr(uploaded_video, "filename", ""):
            video_name = _save_uploaded_video(uploaded_video, user_id, task_id)
        else:
            video_name = _assemble_chunked_video(video_identifier, video_filename, user_id, task_id)
    except (ValueError, FileNotFoundError) as exc:
        return _json_response({"ok": False, "error": str(exc)}, 400)

    extracted_path, game_meta_path, game_flow_path = _resolve_paths(user_id)

    init_db(DB_PATH)
    upsert_user(DB_PATH, user_id, user_name=user_name, status="processing")
    upsert_user_video(
        DB_PATH,
        user_id=user_id,
        video_name=video_name,
        fields={
            "user_id": user_id,
            "video_name": video_name,
            "video_path": os.path.join(VIDEOS_REL_DIR, video_name),
            "status": "processing",
            "extracted_context_path": extracted_path,
            "gamemeta_path": game_meta_path,
            "gameflow_path": game_flow_path,
        },
    )
    set_pipeline_state(
        DB_PATH,
        user_id,
        video_name,
        {
            "events": "pending",
            "entities": "pending",
            "frames": "pending",
            "aigc": "pending",
            "unity": "pending",
            "last_error": None,
        },
    )
    _write_status_json(
        user_id,
        _status_json_payload(
            user_id=user_id,
            task_id=task_id,
            job_id=job_id,
            video_name=video_name,
            status="processing",
            ready=False,
            progress=5,
            current_step="正在分析视频",
            pipeline_state=_default_pipeline_state(),
            error=None,
        ),
    )

    _launch_pipeline_process(
        user_id=user_id,
        video_name=video_name,
        activity=activity,
        people=people,
        activity_time=activity_time,
        location=location,
        confusion_count=confusion_count,
    )

    return _json_response(
        {
            "ok": True,
            "user": user_id,
            "task_id": task_id,
            "job_id": job_id,
            "video": video_name,
            "status": "processing",
            "ready": False,
            "progress": 5,
            "current_step": "正在分析视频",
            "error": None,
        },
        202,
    )


@app.post("/api/tasks/regenerate-images")
def regenerate_images_task():
    payload = request.get_json(silent=True) or {}
    user_id = (payload.get("user") or request.args.get("user") or "default").strip() or "default"
    video_name = os.path.basename((payload.get("video") or request.args.get("video") or "").strip())
    confusion_count_raw = str(
        payload.get("confusion_event_count")
        or request.args.get("confusion_event_count")
        or ""
    ).strip()
    confusion_count: Optional[int] = None
    if confusion_count_raw:
        try:
            confusion_count = _parse_positive_int(confusion_count_raw, "confusion_event_count", allow_zero=True)
        except ValueError as exc:
            return _json_response({"ok": False, "error": str(exc)}, 400)

    init_db(DB_PATH)
    rec = get_video_record(DB_PATH, user_id, video_name) if video_name else get_latest_video_record(DB_PATH, user_id)
    if not rec:
        return _json_response({"ok": False, "error": "task not found"}, 404)

    resolved_video_name = (rec.get("video_name") or video_name or "").strip()
    if not resolved_video_name:
        return _json_response({"ok": False, "error": "video not found"}, 404)

    extracted_path = (rec.get("extracted_context_path") or _resolve_paths(user_id)[0] or "").strip()
    if not extracted_path or not os.path.exists(extracted_path):
        return _json_response({"ok": False, "error": "extracted_context.json not found"}, 404)

    try:
        with open(extracted_path, "r", encoding="utf-8") as f:
            context_data = json.load(f)
    except Exception as exc:
        return _json_response({"ok": False, "error": f"failed to load extracted_context.json: {exc}"}, 500)

    activity = (context_data.get("activity") or str(get_config_value("pipeline.default_activity", "shopping"))).strip()
    people = (context_data.get("people") or str(get_config_value("pipeline.default_people", "me"))).strip()
    activity_time = (context_data.get("time") or str(get_config_value("pipeline.default_time", "2024-01-05 15:00"))).strip()
    location = (context_data.get("location") or str(get_config_value("pipeline.default_location", "supermarket"))).strip()

    status_doc = _read_status_json(user_id)
    task_id = status_doc.get("task_id") or f"task_{_safe_name_component(user_id, 'user')}"
    job_id = status_doc.get("job_id") or task_id
    pipeline_state = get_pipeline_state(DB_PATH, user_id, resolved_video_name)
    pipeline_state.update(
        {
            "frames": "pending",
            "aigc": "pending",
            "unity": "pending",
            "last_error": None,
        }
    )
    set_pipeline_state(DB_PATH, user_id, resolved_video_name, pipeline_state)
    upsert_user(DB_PATH, user_id, status="processing")
    upsert_user_video(
        DB_PATH,
        user_id=user_id,
        video_name=resolved_video_name,
        fields={"status": "processing"},
    )
    _write_status_json(
        user_id,
        _status_json_payload(
            user_id=user_id,
            task_id=task_id,
            job_id=job_id,
            video_name=resolved_video_name,
            status="processing",
            ready=False,
            progress=45,
            current_step="开始重新生成增强图片",
            pipeline_state=pipeline_state,
            error=None,
        ),
    )

    _launch_pipeline_process(
        user_id=user_id,
        video_name=resolved_video_name,
        activity=activity,
        people=people,
        activity_time=activity_time,
        location=location,
        confusion_count=confusion_count,
        force_regenerate_images=True,
    )

    return _json_response(
        {
            "ok": True,
            "user": user_id,
            "video": resolved_video_name,
            "status": "processing",
            "task_id": task_id,
            "job_id": job_id,
            "current_step": "开始重新生成增强图片",
        },
        202,
    )


@app.get("/api/health")
def health():
    # 健康检查接口
    return jsonify({"status": "ok"})


@app.get("/api/users")
def get_users():
    init_db(DB_PATH)
    users = [_build_user_list_item(user) for user in list_users(DB_PATH)]
    return _json_response({"ok": True, "users": users})


@app.get("/api/game-meta")
def get_game_meta():
    # 获取 GameMeta.json（可通过 refresh=1 强制刷新）
    base_url = _get_base_url()
    force = request.args.get("refresh") == "1"
    user_id = request.args.get("user", "default")
    logger.info("GET /api/game-meta user=%s refresh=%s", user_id, force)
    game_meta, _ = _ensure_generated(base_url, user_id=user_id, force=force)
    return _json_response(game_meta)


@app.get("/api/game-flow")
def get_game_flow():
    # 获取 GameFlow.json（可通过 refresh=1 强制刷新）
    base_url = _get_base_url()
    force = request.args.get("refresh") == "1"
    user_id = request.args.get("user", "default")
    logger.info("GET /api/game-flow user=%s refresh=%s", user_id, force)
    _, game_flow = _ensure_generated(base_url, user_id=user_id, force=force)
    return _json_response(game_flow)


@app.get("/api/context")
def get_context():
    # 获取 extracted_context.json 原始内容
    user_id = request.args.get("user", "default")
    extracted_path, _, _ = _resolve_paths(user_id)
    if not os.path.exists(extracted_path):
        return jsonify({"error": "extracted_context.json not found"}), 404
    with open(extracted_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _json_response(data)


@app.get("/api/job-status")
def get_job_status():
    # 查询当前视频处理进度
    base_url = _get_base_url()
    user_id = request.args.get("user", "default").strip() or "default"
    video_name = (request.args.get("video") or "").strip()
    logger.info("GET /api/job-status user=%s video=%s", user_id, video_name or "<latest>")

    status_doc = _read_status_json(user_id)
    if status_doc:
        status_video = (status_doc.get("video") or "").strip()
        if not video_name or not status_video or status_video == video_name:
            return jsonify(_build_job_status_from_status_doc(base_url, user_id, status_doc))

    rec = get_video_record(DB_PATH, user_id, video_name) if video_name else get_latest_video_record(DB_PATH, user_id)
    if not rec:
        return jsonify({
            "ok": False,
            "user": user_id,
            "task_id": "",
            "job_id": "",
            "video": video_name,
            "status": "not_found",
            "progress": 0,
            "current_step": "等待处理",
            "ready": False,
            "pipeline_state": _default_pipeline_state(),
            "error": "job not found",
            "game_meta_url": "",
            "game_flow_url": "",
        }), 404

    resolved_video_name = rec.get("video_name") or video_name
    return jsonify(_build_job_status_payload(base_url, user_id, resolved_video_name, rec))


@app.delete("/api/delete-task")
def delete_task():
    user_id = (request.args.get("user") or "").strip()
    requested_video = os.path.basename((request.args.get("video") or "").strip())
    confirm = (request.args.get("confirm") or "").strip().lower()
    requested_status = _normalize_delete_status(request.args.get("status") or "")
    requested_bucket = _delete_status_bucket(requested_status)

    if not user_id:
        return _json_response({"ok": False, "error": "user is required"}, 400)
    if confirm not in {"true", "1", "yes"}:
        return _json_response({"ok": False, "error": "confirm=true is required"}, 400)
    if not requested_status:
        return _json_response({"ok": False, "error": "status is required"}, 400)

    init_db(DB_PATH)
    rec = get_video_record(DB_PATH, user_id, requested_video) if requested_video else get_latest_video_record(DB_PATH, user_id)
    status_doc = _read_status_json(user_id)

    if not rec and not status_doc:
        return _json_response({"ok": False, "error": "task not found"}, 404)

    current_status = ""
    if rec:
        current_status = _normalize_delete_status(rec.get("status") or "")
    if not current_status and status_doc:
        current_status = _normalize_delete_status(status_doc.get("status") or "")
    current_bucket = _delete_status_bucket(current_status)

    if current_bucket and requested_bucket != current_bucket:
        return _json_response(
            {
                "ok": False,
                "error": "status mismatch",
                "expected_status": current_status,
                "expected_status_bucket": current_bucket,
                "requested_status": requested_status,
                "requested_status_bucket": requested_bucket,
            },
            409,
        )

    resolved_video = requested_video or ((rec or {}).get("video_name") or "")
    if rec and requested_video and requested_video != (rec.get("video_name") or ""):
        return _json_response({"ok": False, "error": "task not found"}, 404)

    video_path = ""
    if rec and rec.get("video_path"):
        video_path = resolve_backend_path(str(rec.get("video_path")))
    elif resolved_video:
        video_path = os.path.join(VIDEOS_DIR, resolved_video)

    cleanup_warnings: list[str] = []
    if video_path:
        try:
            _remove_file_if_within(VIDEOS_DIR, video_path)
        except Exception as exc:
            logger.warning("delete-task video cleanup failed user=%s video=%s err=%s", user_id, resolved_video, exc)
            cleanup_warnings.append(f"video_cleanup_failed: {exc}")
    try:
        _remove_tree_if_within(OUTPUT_ROOT, _resolve_output_dir(user_id))
    except Exception as exc:
        logger.warning("delete-task output cleanup failed user=%s err=%s", user_id, exc)
        cleanup_warnings.append(f"output_cleanup_failed: {exc}")

    deleted_rows = delete_user_task(DB_PATH, user_id, resolved_video or None)
    if deleted_rows <= 0 and rec:
        return _json_response({"ok": False, "error": "task delete failed"}, 500)

    return _json_response(
        {
            "ok": True,
            "user": user_id,
            "video": resolved_video,
            "deleted_rows": deleted_rows,
            "cleanup_warnings": cleanup_warnings,
        }
    )


@app.post("/api/recall-report")
def post_recall_report():
    user_id = request.args.get("user", "default").strip() or "default"
    logger.info("POST /api/recall-report user=%s", user_id)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "json body is required"}), 400

    out_dir = _resolve_output_dir(user_id)
    os.makedirs(out_dir, exist_ok=True)
    report_path = _resolve_recall_report_path(user_id)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return jsonify({
        "ok": True,
        "user": user_id,
        "path": report_path,
    })


@app.get("/api/recall-report")
def get_recall_report():
    user_id = request.args.get("user", "default").strip() or "default"
    logger.info("GET /api/recall-report user=%s", user_id)

    report_path = _resolve_recall_report_path(user_id)
    if not os.path.exists(report_path):
        return jsonify({"ok": False, "error": "recall_report.json not found"}), 404

    with open(report_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return _json_response(data)


@app.post("/api/tts")
def tts():
    # 文本转语音（Edge-TTS），生成后缓存到 output/{user}/tts
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or request.form.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    if len(text) > 1000:
        return jsonify({"error": "text too long (max 1000 chars)"}), 400

    user_id = data.get("user") or request.args.get("user", "default")
    voice = data.get("voice", "zh-CN-XiaoxiaoNeural")
    rate = data.get("rate", "+0%")
    volume = data.get("volume", "+0%")
    fmt = (data.get("format", "mp3") or "mp3").lstrip(".").lower()
    if fmt != "mp3":
        fmt = "mp3"

    cache_dir = _resolve_tts_dir(user_id)
    os.makedirs(cache_dir, exist_ok=True)

    key = _build_tts_cache_key(text, voice, rate, volume, fmt)
    filename = f"{key}.{fmt}"
    out_path = os.path.join(cache_dir, filename)

    cached = os.path.exists(out_path)
    if cached:
        ok, err = _validate_tts_file(out_path)
        if not ok:
            try:
                os.remove(out_path)
                logger.warning("invalid cached tts removed: %s, reason=%s", out_path, err)
            except OSError as exc:
                logger.warning("failed to remove invalid cached tts: %s, error=%s", out_path, exc)
            cached = False

    tts_ms = 0
    if not cached:
        try:
            tts_start = time.perf_counter()
            asyncio.run(_edge_tts_save(text, voice, rate, volume, out_path))
            tts_ms = int((time.perf_counter() - tts_start) * 1000)
        except RuntimeError as exc:
            return jsonify({
                "ok": False,
                "status": "fatal_error",
                "request_id": key,
                "error_code": "tts_engine_missing",
                "error_message": str(exc),
                "retry_after_ms": 0
            }), 501

        except Exception as exc:
            return jsonify({
                "ok": False,
                "status": "retryable_error",
                "request_id": key,
                "error_code": "tts_generate_failed",
                "error_message": str(exc),
                "retry_after_ms": 1000
            }), 500

    base_url = _get_base_url()
    rel_path = f"tts/{filename}"
    url = f"{base_url}/assets/{user_id}/{rel_path}"
    total_ms = int((time.perf_counter() - request.start_time) * 1000) if hasattr(request, "start_time") else None

    ok, err = _validate_tts_file(out_path)
    if not ok:
        return jsonify({
            "ok": False,
            "status": "retryable_error",
            "request_id": key,
            "url": "",
            "cached": False,
            "path": "",
            "voice": voice,
            "rate": rate,
            "volume": volume,
            "format": fmt,
            "file_size": 0,
            "retry_after_ms": 500,
            "tts_generate_ms": tts_ms,
            "server_total_ms": total_ms,
            "error_code": err,
            "error_message": "tts file not ready"
        }), 503
    
    return jsonify({
    "ok": True,
    "status": "ready",            # ready / retryable_error / fatal_error
    "request_id": key,
    "url": url,
    "cached": cached,
    "path": rel_path,
    "voice": voice,
    "rate": rate,
    "volume": volume,
    "format": fmt,
    "file_size": os.path.getsize(out_path) if os.path.exists(out_path) else 0,
    "retry_after_ms": 0,
    "tts_generate_ms": tts_ms,
    "server_total_ms": total_ms,
    "error_code": "",
    "error_message": ""
})


@app.get("/assets/<user_id>/<path:subpath>")
def get_asset(user_id: str, subpath: str):
    full_dir = _resolve_output_dir(user_id)
    full_path = os.path.join(full_dir, subpath)
    logger.info("GET /assets/%s/%s -> %s", user_id, subpath, full_path)

    if not os.path.exists(full_path):
        logger.warning("asset not found: %s", full_path)
        return jsonify({
            "ok": False,
            "status": "retryable_error",
            "error_code": "asset_not_found",
            "error_message": "asset file not found",
            "retry_after_ms": 500
        }), 404

    size = os.path.getsize(full_path)
    if size <= 0:
        logger.warning("asset empty: %s", full_path)
        return jsonify({
            "ok": False,
            "status": "retryable_error",
            "error_code": "asset_empty",
            "error_message": "asset file is empty",
            "retry_after_ms": 500
        }), 503

    response = send_from_directory(full_dir, subpath)
    logger.info("asset served: %s (%s bytes)", full_path, size)

    mime, _ = mimetypes.guess_type(full_path)
    if mime:
        response.headers["Content-Type"] = mime

    response.headers["Cache-Control"] = "no-cache"
    return response


if __name__ == "__main__":
    # 启动开发服务器
    init_db(DB_PATH)
    port = int(get_config_value("server.port", 8000))
    host = str(get_config_value("server.host", "0.0.0.0"))
    app.run(host=host, port=port, debug=True)
