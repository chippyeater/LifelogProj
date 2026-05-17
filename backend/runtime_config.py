import copy
import json
import os
from typing import Any, Dict
from dotenv import load_dotenv


BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BACKEND_DIR, "runtime_config.json")
DOTENV_PATH = os.path.join(BACKEND_DIR, ".env")

load_dotenv(dotenv_path=DOTENV_PATH)


DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "api_base_url": "",
        "database_path": "lifelog.db",
    },
    "paths": {
        "output_root": "output",
        "videos_dir": "videos",
        "upload_chunks_dir": "upload_chunks",
        "prompts_dir": "prompts",
        "temp_dir": "tmp",
        "huggingface_root": "",
        "pipeline_cache_dirname": "pipeline_cache",
        "logs_dirname": "logs",
        "siliconflow_log_path": "output/siliconflow_responses.txt",
        "material_library_root": "output",
    },
    "binaries": {
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
    },
    "pipeline": {
        "default_activity": "逛超市",
        "default_people": "我",
        "default_time": "2024年1月5日15:00",
        "default_location": "超市",
        "output_root": "output",
        "pipeline_mode": "staged",
        "start_stage": 1,
        "end_stage": None,
        "confusion_count": 2,
        "force_regenerate_images": False,
    },
    "video_processing": {
        "compression": {
            "enabled_by_default": False,
            "scale_width": -2,
            "scale_height": 720,
            "frame_rate": 15,
            "video_codec": "libx264",
            "video_bitrate": "2M",
            "video_pixel_format": "yuv420p",
            "video_profile": "high",
            "video_level": "4.0",
            "audio_codec": "aac",
            "audio_bitrate": "128k",
            "preset": "",
            "overwrite": True,
            "keep_audio": True,
            "movflags": "+faststart",
            "output_subdir": "compressed",
            "filename_prefix": "compressed",
        },
        "media_export": {
            "activity_audio_min_seconds": 3,
            "activity_audio_max_seconds": 8,
            "event_video_min_seconds": 4,
            "event_video_max_seconds": 10,
            "video_codec": "libx264",
            "video_pixel_format": "yuv420p",
            "video_profile": "high",
            "video_level": "4.0",
            "video_preset": "medium",
            "video_crf": 23,
            "audio_codec": "aac",
            "audio_bitrate": "128k",
            "audio_sample_rate": 44100,
            "audio_channels": 2,
            "movflags": "+faststart",
        },
    },
    "models": {
        "siliconflow": {
            "api_key": "",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "tencent/Hunyuan-MT-7B",
            "temperature": 0.2,
            "max_tokens": 4000,
            "translate_max_tokens": 1000,
            "timeout_seconds": 120,
            "log_path": "output/siliconflow_responses.txt",
        },
        "bailian": {
            "api_key": "",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.5-flash",
            "max_tokens": 4096,
            "temperature": 0.2,
            "stage2_model": "",
            "stage2_max_tokens": 4096,
            "stage2_temperature": 0.2,
            "stage3_model": "",
            "stage3_max_tokens": 4096,
            "stage3_temperature": 0.2,
            "input_mode": "text",
            "upload_api_url": "https://dashscope.aliyuncs.com/api/v1/uploads",
            "use_temp_upload": True,
            "policy_timeout_seconds": 60,
            "upload_timeout_seconds": 300,
            "max_video_duration_seconds": None,
            "max_video_size_bytes": None,
        },
        "unity_generation": {
            "confusion_event_batch_model": "qwen3.6-plus",
            "detail_distractor_batch_model": "qwen3.6-plus",
        },
        "embedding": {
            "model_name": "moka-ai/m3e-base",
            "event_similarity_threshold": 0.9,
            "entity_similarity_threshold": 0.9,
        },
        "volcengine": {
            "access_key": "",
            "secret_key": "",
        },
    },
    "aigc": {
        "max_retry": 3,
        "poll_interval_seconds": 3,
        "timeout_seconds": 30,
        "download_timeout_seconds": 10,
        "prompt_max_chars": 800,
        "option_image_width": 512,
        "option_image_height": 512,
        "option_image_scale": 2.5,
        "add_logo": False,
        "add_aigc_meta": True,
    },
    "unity": {
        "activity_option_total": 3,
        "detail_option_total": 4,
        "confusion_material_subdir": "default",
    },
    "prompts": {
        "full_context": "prompts/full_context_prompt.md",
        "stage1_video_parse": "prompts/stage1_video_parse_prompt.md",
        "stage2_event_rebuild": "prompts/stage2_event_rebuild_prompt.md",
        "stage3_detail_generate": "prompts/stage3_detail_generate_prompt.md",
        "confusion_event_batch": "prompts/confusion_event_batch_prompt.md",
        "detail_distractor_batch": "prompts/detail_distractor_batch_prompt.md",
        "strategy": "prompts/strategy_prompt.md",
        "strategy_components": "prompts/strategy_components.json",
    },
    "strategy": {
        "response_timeout_seconds": 10,
    },
}


ENV_ONLY_OVERRIDES = {
    "server.api_base_url": "PUBLIC_BASE_URL",
    "models.siliconflow.api_key": "SILICONFLOW_API_KEY",
    "models.bailian.api_key": "BAILIAN_API_KEY",
    "models.volcengine.access_key": "VOLC_ACCESS_KEY",
    "models.volcengine.secret_key": "VOLC_SECRET_KEY",
}

SENSITIVE_CONFIG_PATHS = tuple(ENV_ONLY_OVERRIDES.keys())


_FILE_CONFIG_CACHE: Dict[str, Any] | None = None


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _set_nested(config: Dict[str, Any], dotted_path: str, value: Any) -> None:
    parts = dotted_path.split(".")
    cursor = config
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def _get_nested(config: Dict[str, Any], dotted_path: str, default: Any = None) -> Any:
    cursor: Any = config
    for part in dotted_path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def _coerce_value(raw_value: str, current_value: Any) -> Any:
    if isinstance(current_value, bool):
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current_value, int) and not isinstance(current_value, bool):
        return int(raw_value)
    if isinstance(current_value, float):
        return float(raw_value)
    if current_value is None:
        lowered = raw_value.strip().lower()
        if lowered in {"null", "none"}:
            return None
        return raw_value
    return raw_value


def _load_file_config() -> Dict[str, Any]:
    config_path = os.getenv("RUNTIME_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    if not os.path.exists(config_path):
        return copy.deepcopy(DEFAULT_CONFIG)
    with open(config_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Runtime config must be a JSON object: {config_path}")
    merged = _deep_merge(DEFAULT_CONFIG, payload)
    for dotted_path in SENSITIVE_CONFIG_PATHS:
        default_value = _get_nested(DEFAULT_CONFIG, dotted_path)
        _set_nested(merged, dotted_path, copy.deepcopy(default_value))
    return merged


def get_runtime_config(force_reload: bool = False) -> Dict[str, Any]:
    global _FILE_CONFIG_CACHE
    if force_reload or _FILE_CONFIG_CACHE is None:
        _FILE_CONFIG_CACHE = _load_file_config()
    config = copy.deepcopy(_FILE_CONFIG_CACHE)
    for dotted_path, env_name in ENV_ONLY_OVERRIDES.items():
        raw_value = os.getenv(env_name)
        if raw_value is None or raw_value == "":
            continue
        current_value = _get_nested(config, dotted_path)
        _set_nested(config, dotted_path, _coerce_value(raw_value, current_value))
    if not _get_nested(config, "models.bailian.api_key"):
        dashscope_key = os.getenv("DASHSCOPE_API_KEY")
        if dashscope_key:
            _set_nested(config, "models.bailian.api_key", dashscope_key)
    return config


def get_config_value(dotted_path: str, default: Any = None) -> Any:
    return _get_nested(get_runtime_config(), dotted_path, default)


def apply_huggingface_cache_config() -> str:
    root = str(get_config_value("paths.huggingface_root", "") or "").strip()
    if not root:
        return ""
    resolved_root = resolve_backend_path(root)
    os.environ["HF_HOME"] = resolved_root
    os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(resolved_root, "hub")
    os.environ["TRANSFORMERS_CACHE"] = os.path.join(resolved_root, "transformers")
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = os.path.join(resolved_root, "sentence_transformers")
    return resolved_root


def resolve_backend_path(path_value: str | None) -> str:
    if not path_value:
        return BACKEND_DIR
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(BACKEND_DIR, path_value)


def resolve_prompt_path(prompt_key: str) -> str:
    prompt_path = get_config_value(f"prompts.{prompt_key}")
    return resolve_backend_path(prompt_path)
