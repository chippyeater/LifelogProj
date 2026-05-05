import json
import os
import asyncio
import hashlib
import time
import logging
import re
import sys
import uuid
import subprocess
from typing import Tuple, Dict, Any, Optional

from flask import Flask, Response, jsonify, send_from_directory, request, abort
import mimetypes
from generate_unity_json import generate_game_meta_flow, validate_generated_assets
from db import get_pipeline_state, get_video_record, get_latest_video_record, upsert_user_video, init_db, set_pipeline_state

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ROOT = os.path.join(APP_DIR, "output")
DB_PATH = os.getenv("PIPELINE_DB_PATH", os.path.join(APP_DIR, "lifelog.db"))

app = Flask(__name__)
logger = logging.getLogger(__name__)
TASK_STATUS_FILENAME = "status.json"


@app.before_request
def _mark_request_start():
    request.start_time = time.perf_counter()


def _normalize_base_url(base_url: str) -> str:
    # 规范化基础 URL，移除末尾斜杠，避免重复拼接
    return base_url.rstrip("/")


def _get_base_url() -> str:
    # 获取对外可访问的基础 URL（优先环境变量，否则使用请求 host）
    env_base = os.getenv("API_BASE_URL")
    if env_base:
        return _normalize_base_url(env_base)
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


def _save_uploaded_video(file_storage, user_id: str, task_id: str) -> str:
    videos_dir = os.path.join(APP_DIR, "videos")
    os.makedirs(videos_dir, exist_ok=True)
    original_name = os.path.basename((getattr(file_storage, "filename", "") or "").strip())
    stem, ext = os.path.splitext(original_name)
    ext = ext or ".mp4"
    safe_stem = _safe_name_component(stem, "upload")
    video_name = f"{_safe_name_component(user_id, 'user')}_{task_id}_{safe_stem}{ext}"
    save_path = os.path.join(videos_dir, video_name)
    file_storage.save(save_path)
    return video_name


def _launch_pipeline_process(
    *,
    user_id: str,
    video_name: str,
    activity: str,
    people: str,
    activity_time: str,
    location: str,
) -> None:
    cmd = [
        sys.executable,
        os.path.join(APP_DIR, "run_pipeline.py"),
        "--user",
        user_id,
        "--video",
        video_name,
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
    if force or _should_regenerate(extracted_path, game_meta_path, game_flow_path):
        return generate_game_meta_flow(
            input_path=extracted_path,
            output_dir=output_dir,
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


@app.post("/api/tasks/process")
def create_process_task():
    user_id = (request.form.get("user") or "default").strip() or "default"
    uploaded_video = request.files.get("video")
    if uploaded_video is None or not getattr(uploaded_video, "filename", ""):
        return _json_response({"ok": False, "error": "video is required"}, 400)

    activity = (request.form.get("activity") or "逛超市").strip() or "逛超市"
    people = (request.form.get("people") or "我").strip() or "我"
    activity_time = (request.form.get("time") or "2024年1月5日15:00").strip() or "2024年1月5日15:00"
    location = (request.form.get("location") or "超市").strip() or "超市"
    shopping_list_raw = (request.form.get("shopping_list") or "").strip()

    task_id = _make_task_id(user_id)
    job_id = task_id
    video_name = _save_uploaded_video(uploaded_video, user_id, task_id)
    extracted_path, game_meta_path, game_flow_path = _resolve_paths(user_id)

    init_db(DB_PATH)
    upsert_user_video(
        DB_PATH,
        user_id=user_id,
        video_name=video_name,
        fields={
            "user_id": user_id,
            "video_name": video_name,
            "video_path": os.path.join("videos", video_name),
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
        {
            "user": user_id,
            "task_id": task_id,
            "job_id": job_id,
            "video": video_name,
            "status": "processing",
            "current_step": "正在分析视频",
            "progress": 5,
            "error": None,
            "shopping_list": shopping_list_raw,
        },
    )

    _launch_pipeline_process(
        user_id=user_id,
        video_name=video_name,
        activity=activity,
        people=people,
        activity_time=activity_time,
        location=location,
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


@app.get("/api/health")
def health():
    # 健康检查接口
    return jsonify({"status": "ok"})


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
    # 查询某个视频任务的处理状态，供 Unity 轮询
    base_url = _get_base_url()
    user_id = request.args.get("user", "default").strip() or "default"
    video_name = (request.args.get("video") or "").strip()
    logger.info("GET /api/job-status user=%s video=%s", user_id, video_name or "<latest>")

    rec = get_video_record(DB_PATH, user_id, video_name) if video_name else get_latest_video_record(DB_PATH, user_id)
    if not rec:
        return jsonify({
            "ok": False,
            "user": user_id,
            "task_id": "",
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
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=True)
