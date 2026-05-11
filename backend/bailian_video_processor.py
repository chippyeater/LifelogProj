import json
import os
import inspect
import logging
import base64
import mimetypes
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import requests
from openai import OpenAI
from jinja2 import Template

from my_basics import ActivityContext, parse_json_from_llm
from pipeline_schema import (
    validate_stage1_schema,
    validate_stage2_schema,
    validate_stage3_schema,
)
from runtime_config import get_config_value, resolve_prompt_path

logger = logging.getLogger(__name__)

DEFAULT_MAX_VIDEO_SIZE_BYTES = 2 * 1024 * 1024 * 1024
MODEL_VIDEO_LIMITS = (
    (("qwen3.6-plus", "qwen3.6-flash", "qwen3.5-plus", "qwen3.5-flash"), 2 * 60 * 60, DEFAULT_MAX_VIDEO_SIZE_BYTES),
    (("qwen3-vl-plus", "qwen3-vl-flash"), 60 * 60, DEFAULT_MAX_VIDEO_SIZE_BYTES),
    (("qwen3.5-omni-plus", "qwen3.5-omni-flash"), 60 * 60, DEFAULT_MAX_VIDEO_SIZE_BYTES),
)


def _resolve_model_video_limits(model_name: str) -> tuple[int | None, int | None]:
    normalized = (model_name or "").strip().lower()
    for prefixes, max_duration_seconds, max_size_bytes in MODEL_VIDEO_LIMITS:
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return max_duration_seconds, max_size_bytes
    return None, None


def _probe_local_video_duration_seconds(video_path: str) -> float | None:
    ffprobe = str(get_config_value("binaries.ffprobe_bin", "ffprobe"))
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        output = (result.stdout or "").strip()
        return float(output) if output else None
    except Exception:
        return None


def _normalize_stage1_video_parse(data: Dict[str, Any], ctx: ActivityContext) -> Dict[str, Any]:
    # 鎶婃ā鍨嬭緭鍑哄綊涓€鍖栨垚闃舵1鏍囧噯缁撴瀯锛屽厛淇濈粨鏋勭ǔ瀹氾紝涓嶅仛澶嶆潅璇箟淇
    if not isinstance(data, dict):
        data = {}

    timeline = data.get("timeline")
    if not isinstance(timeline, list):
        timeline = []

    normalized_timeline = []
    frame_counter = 0
    for segment in timeline:
        if not isinstance(segment, dict):
            continue
        keyframes = segment.get("keyframes")
        if not isinstance(keyframes, list):
            keyframes = []

        normalized_keyframes = []
        for frame in keyframes:
            if not isinstance(frame, dict):
                continue
            candidates = frame.get("interaction_candidates")
            if not isinstance(candidates, list):
                candidates = []
            normalized_candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                normalized_candidates.append(
                    {
                        "object_name": candidate.get("object_name", ""),
                        "action_name": candidate.get("action_name", ""),
                    }
                )

            frame_counter += 1
            normalized_keyframes.append(
                {
                    "frame_id": f"f_{frame_counter:03d}",
                    "frame_time": frame.get("frame_time", ""),
                    "interaction_candidates": normalized_candidates,
                }
            )

        normalized_timeline.append(
            {
                "start_time": segment.get("start_time", ""),
                "end_time": segment.get("end_time", ""),
                "behavior_goal": segment.get("behavior_goal", ""),
                "segment_description": segment.get("segment_description", ""),
                "reason": segment.get("reason", ""),
                "keyframes": normalized_keyframes,
            }
        )

    normalized = {
        "schema_version": "stage1_v1",
        "metadata": {
            "activity_name": ctx.activity,
            "time_info": ctx.time,
            "location_info": ctx.location,
            "source_video": "",
            "source": "manual",
        },
        "timeline": normalized_timeline,
    }
    return normalized


def _normalize_stage2_event_rebuild(data: Dict[str, Any], stage1_output: Dict[str, Any]) -> Dict[str, Any]:
    # 把模型输出归一化成新的阶段2结构：事件重建和实体筛选在同一轮完成
    if not isinstance(data, dict):
        data = {}

    metadata = stage1_output.get("metadata") if isinstance(stage1_output.get("metadata"), dict) else {}
    events = data.get("events")
    if not isinstance(events, list):
        events = []

    normalized_events = []
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        selected_entities = event.get("selected_entities")
        if not isinstance(selected_entities, list):
            selected_entities = []

        normalized_entities = []
        event_id = f"e_{index:03d}"
        for entity_index, entity in enumerate(selected_entities, start=1):
            if not isinstance(entity, dict):
                continue
            normalized_entities.append(
                {
                    "entity_id": f"{event_id}_ent_{entity_index:03d}",
                    "entity_name": entity.get("entity_name", ""),
                    "entity_type": entity.get("entity_type", ""),
                    "selection_reason": entity.get("selection_reason", ""),
                    "evidence_frame_ids": entity.get("evidence_frame_ids", []),
                    "anchor_frame_id": entity.get("anchor_frame_id", ""),
                }
            )

        normalized_events.append(
            {
                "event_id": event_id,
                "event_name": event.get("event_name", ""),
                "event_description": event.get("event_description", ""),
                "start_time": event.get("start_time", ""),
                "end_time": event.get("end_time", ""),
                "reason": event.get("reason", ""),
                "source_segment_indices": event.get("source_segment_indices", []),
                "representative_frame_ids": event.get("representative_frame_ids", []),
                "selected_entities": normalized_entities,
            }
        )

    return {
        "schema_version": "stage2_v1",
        "metadata": {
            "activity_name": metadata.get("activity_name", ""),
            "time_info": metadata.get("time_info", ""),
            "location_info": metadata.get("location_info", ""),
            "source_video": metadata.get("source_video", ""),
            "source": "stage1",
        },
        "events": normalized_events,
    }


def _build_stage1_frame_map(stage1_output: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    # 寤虹珛 frame_id 鍒板叧閿抚淇℃伅鐨勬槧灏勶紝渚涢樁娈?鍚庡鐞嗗幓閲嶅拰璇佹嵁鍒嗘瀽
    frame_map: Dict[str, Dict[str, Any]] = {}
    timeline = stage1_output.get("timeline")
    if not isinstance(timeline, list):
        return frame_map

    for segment in timeline:
        if not isinstance(segment, dict):
            continue
        for keyframe in segment.get("keyframes") or []:
            if not isinstance(keyframe, dict):
                continue
            frame_id = keyframe.get("frame_id")
            if not frame_id:
                continue
            frame_map[frame_id] = keyframe
    return frame_map


def _resolve_entity_action_signature(entity: Dict[str, Any], frame_map: Dict[str, Dict[str, Any]]) -> str:
    # 浠庨敋鐐瑰抚閲屾壘涓庡綋鍓嶅疄浣撳悕绉版渶鍖归厤鐨勫姩浣滐紝浣滀负璺ㄤ簨浠跺幓閲嶇殑杞婚噺渚濇嵁
    anchor_frame_id = entity.get("anchor_frame_id", "")
    frame = frame_map.get(anchor_frame_id) or {}
    candidates = frame.get("interaction_candidates")
    if not isinstance(candidates, list):
        return ""

    entity_name = str(entity.get("entity_name", "")).strip()
    matched_actions: List[str] = []
    fallback_actions: List[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        object_name = str(candidate.get("object_name", "")).strip()
        action_name = str(candidate.get("action_name", "")).strip()
        if not action_name:
            continue
        fallback_actions.append(action_name)
        if entity_name and object_name == entity_name:
            matched_actions.append(action_name)

    actions = matched_actions or fallback_actions
    return "|".join(sorted(set(actions)))


def _post_filter_stage2_entities(
    stage2_output: Dict[str, Any],
    stage1_output: Dict[str, Any],
) -> Dict[str, Any]:
    # 做一层轻量程序筛选，压掉跨事件重复且主交互动作不变的实体
    frame_map = _build_stage1_frame_map(stage1_output)
    seen_signatures: set[tuple[str, str, str]] = set()

    for event in stage2_output.get("events") or []:
        if not isinstance(event, dict):
            continue
        selected_entities = event.get("selected_entities")
        if not isinstance(selected_entities, list):
            continue

        filtered_entities = []
        for entity in selected_entities:
            if not isinstance(entity, dict):
                continue
            entity_type = str(entity.get("entity_type", "")).upper()
            entity_name = str(entity.get("entity_name", "")).strip()

            action_signature = _resolve_entity_action_signature(entity, frame_map)
            dedupe_key = (entity_type, entity_name, action_signature)
            if entity_name and dedupe_key in seen_signatures:
                continue

            if entity_name:
                seen_signatures.add(dedupe_key)
            filtered_entities.append(entity)

        event["selected_entities"] = filtered_entities

    return stage2_output


def _normalize_stage3_detail_generate(
    data: Dict[str, Any],
    stage2_output: Dict[str, Any],
) -> Dict[str, Any]:
    # 把逐实体细节结果归一化成新的阶段3标准结构，detail_id 由程序补齐
    if not isinstance(data, dict):
        data = {}

    metadata = stage2_output.get("metadata") if isinstance(stage2_output.get("metadata"), dict) else {}
    entity_details = data.get("entity_details")
    if not isinstance(entity_details, list):
        entity_details = []

    event_map = {
        event.get("event_id"): event
        for event in (stage2_output.get("events") or [])
        if isinstance(event, dict) and event.get("event_id")
    }

    normalized_entity_details = []
    for entity_bundle in entity_details:
        if not isinstance(entity_bundle, dict):
            continue
        entity_id = entity_bundle.get("entity_id", "")
        event_id = entity_bundle.get("event_id", "")
        stage3_event = event_map.get(event_id, {})
        detail_items = entity_bundle.get("detail_items")
        if not isinstance(detail_items, list):
            detail_items = []

        normalized_items = []
        for detail_index, item in enumerate(detail_items, start=1):
            if not isinstance(item, dict):
                continue
            normalized_items.append(
                {
                    "detail_id": f"{entity_id}_d_{detail_index:03d}" if entity_id else f"d_{detail_index:03d}",
                    "question": item.get("question", ""),
                    "correct_answer": item.get("correct_answer", ""),
                    "distractors": item.get("distractors", []),
                }
            )

        normalized_entity_details.append(
            {
                "entity_id": entity_id,
                "event_id": event_id,
                "event_name": stage3_event.get("event_name", entity_bundle.get("event_name", "")),
                "entity_name": entity_bundle.get("entity_name", ""),
                "anchor_frame_id": entity_bundle.get("anchor_frame_id", ""),
                "detail_items": normalized_items,
            }
        )

    return {
        "schema_version": "stage3_v1",
        "metadata": {
            "activity_name": metadata.get("activity_name", ""),
            "time_info": metadata.get("time_info", ""),
            "location_info": metadata.get("location_info", ""),
            "source_video": metadata.get("source_video", ""),
            "source": "stage2",
        },
        "entity_details": normalized_entity_details,
    }


def _resolve_stage1_image_paths(stage1_output: Dict[str, Any], output_dir: str) -> List[str]:
    # 从阶段1结果里收集关键帧图片绝对路径，供后续多模态阶段复用
    timeline = stage1_output.get("timeline")
    if not isinstance(timeline, list):
        return []

    image_paths: List[str] = []
    seen: set[str] = set()
    for segment in timeline:
        if not isinstance(segment, dict):
            continue
        for keyframe in segment.get("keyframes") or []:
            if not isinstance(keyframe, dict):
                continue
            image_path = keyframe.get("image_path")
            if not image_path:
                continue
            abs_path = image_path if os.path.isabs(image_path) else os.path.join(output_dir, image_path)
            abs_path = os.path.normpath(abs_path)
            if not os.path.exists(abs_path):
                continue
            if abs_path in seen:
                continue
            seen.add(abs_path)
            image_paths.append(abs_path)
    return image_paths


def create_bailian_processor(
    video_url: str | None = None,
    video_path: str | None = None,
    output_dir: str = "output",
) -> "BailianVideoProcessor":
    processor = BailianVideoProcessor(
        video_url=video_url,
        video_path=video_path,
        output_dir=output_dir,
    )
    return processor


class BailianVideoProcessor:
    """
    浣跨敤闃块噷浜戠櫨鐐硷紙OpenAI鍏煎锛夋ā鍨嬪鐞嗚棰戠悊瑙ｃ€?    閫氳繃妯″瀷瀵硅棰慤RL+鎻愮ず璇嶈繘琛岃В鏋愶紝杈撳嚭缁撴瀯鍖朖SON銆?    """

    def __init__(
        self,
        video_url: str | None = None,
        video_path: str | None = None,
        output_dir: str = "output",
    ) -> None:
        if not video_url and not video_path:
            raise ValueError("video_url or video_path is required for Bailian processing.")
        self.video_url = video_url
        self.output_dir = output_dir
        self.local_video_path = video_path

        # Config-managed: model selection, token limits, temperatures, upload mode, and timeouts.
        cfg = get_config_value("models.bailian", {})
        api_key = cfg.get("api_key")
        if not api_key:
            raise ValueError("BAILIAN_API_KEY (or DASHSCOPE_API_KEY) not set.")
        base_url = cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = cfg.get("model", "qwen3.5-flash")
        self.max_tokens = int(cfg.get("max_tokens", 4096))
        self.temperature = float(cfg.get("temperature", 0.2))
        self.stage2_model = cfg.get("stage2_model") or self.model
        self.stage2_max_tokens = int(cfg.get("stage2_max_tokens", self.max_tokens))
        self.stage2_temperature = float(cfg.get("stage2_temperature", self.temperature))
        self.stage3_model = cfg.get("stage3_model") or self.stage2_model
        self.stage3_max_tokens = int(cfg.get("stage3_max_tokens", self.stage2_max_tokens))
        self.stage3_temperature = float(cfg.get("stage3_temperature", self.stage2_temperature))
        self.input_mode = cfg.get("input_mode", "text")
        self.upload_api_url = cfg.get("upload_api_url", "https://dashscope.aliyuncs.com/api/v1/uploads")
        self.use_temp_upload = bool(cfg.get("use_temp_upload", False))
        self.policy_timeout_seconds = float(cfg.get("policy_timeout_seconds", 60))
        self.upload_timeout_seconds = float(cfg.get("upload_timeout_seconds", 300))
        self.max_video_duration_seconds = cfg.get("max_video_duration_seconds")
        self.max_video_size_bytes = cfg.get("max_video_size_bytes")

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self._api_key = api_key

        logger.info(
            "BailianVideoProcessor initialized with model=%s, max_tokens=%s, input_mode=%s, temperature=%s, stage2_model=%s, stage2_max_tokens=%s, stage2_temperature=%s, stage3_model=%s, stage3_max_tokens=%s, stage3_temperature=%s",
            self.model,
            self.max_tokens,
            self.input_mode,
            self.temperature,
            self.stage2_model,
            self.stage2_max_tokens,
            self.stage2_temperature,
            self.stage3_model,
            self.stage3_max_tokens,
            self.stage3_temperature,
        )

        if self.local_video_path:
            self._validate_local_video_limits(self.local_video_path)

        if not self.video_url and video_path and self.use_temp_upload:
            self.video_url = self._upload_local_file_to_temp_oss(video_path)
        if not self.video_url and video_path and not self.use_temp_upload:
            raise ValueError(
                "Bailian requires a video_url, but models.bailian.use_temp_upload is false and no existing video_url was found."
            )
        if not self.video_url:
            raise ValueError("video_url is required for Bailian processing.")

    def _validate_local_video_limits(self, video_path: str) -> None:
        model_duration_limit, model_size_limit = _resolve_model_video_limits(self.model)
        max_duration_seconds = int(self.max_video_duration_seconds) if self.max_video_duration_seconds is not None else model_duration_limit
        max_size_bytes = int(self.max_video_size_bytes) if self.max_video_size_bytes is not None else model_size_limit
        if max_duration_seconds is None and max_size_bytes is None:
            return
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        file_size_bytes = os.path.getsize(video_path)
        if max_size_bytes is not None and file_size_bytes > max_size_bytes:
            raise ValueError(
                f"Video file too large for model {self.model}: "
                f"{file_size_bytes / (1024 * 1024 * 1024):.2f}GB > {max_size_bytes / (1024 * 1024 * 1024):.0f}GB"
            )

        duration_seconds = _probe_local_video_duration_seconds(video_path)
        if max_duration_seconds is not None:
            if duration_seconds is None:
                raise ValueError(
                    f"Unable to determine video duration for model limit validation: {video_path}"
                )
            if duration_seconds > max_duration_seconds:
                raise ValueError(
                    f"Video duration exceeds model {self.model} limit: "
                    f"{duration_seconds / 3600:.2f}h > {max_duration_seconds / 3600:.0f}h"
                )

    def _get_upload_policy(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        params = {"action": "getPolicy", "model": self.model}
        resp = requests.get(self.upload_api_url, headers=headers, params=params, timeout=self.policy_timeout_seconds)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to get upload policy: {resp.status_code} {resp.text}")
        data = resp.json().get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected upload policy response: {resp.text}")
        return data

    def _upload_local_file_to_temp_oss(self, file_path: str) -> str:
        """
        鎶婃湰鍦版枃浠朵紶鍒扮櫨鐐间复鏃?OSS锛岃繑鍥?oss://xxx
        娉ㄦ剰锛氱櫨鐐间細鏈夋枃浠跺ぇ灏忛檺鍒讹紝瓒呴檺浼氭姤閿欍€?        """
        logger.info("Uploading local file to Bailian temp OSS: %s", file_path)
        policy_data = self._get_upload_policy()
        max_file_size_mb = policy_data.get("max_file_size_mb")
        capacity_limit_mb = policy_data.get("capacity_limit_mb")
        logger.info(
            "Bailian temp upload limits: max_file_size_mb=%s capacity_limit_mb=%s",
            max_file_size_mb,
            capacity_limit_mb,
        )
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if isinstance(max_file_size_mb, (int, float)) and file_size_mb > float(max_file_size_mb):
            raise ValueError(
                f"File too large for Bailian temp upload: {file_size_mb:.2f}MB > max_file_size_mb={max_file_size_mb}"
            )
        if isinstance(capacity_limit_mb, (int, float)) and file_size_mb > float(capacity_limit_mb):
            raise ValueError(
                f"File exceeds capacity_limit_mb: {file_size_mb:.2f}MB > capacity_limit_mb={capacity_limit_mb}"
            )
        file_name = Path(file_path).name
        key = f"{policy_data['upload_dir']}/{file_name}"
        with open(file_path, "rb") as f:
            files = {
                "OSSAccessKeyId": (None, policy_data["oss_access_key_id"]),
                "Signature": (None, policy_data["signature"]),
                "policy": (None, policy_data["policy"]),
                "x-oss-object-acl": (None, policy_data["x_oss_object_acl"]),
                "x-oss-forbid-overwrite": (None, policy_data["x_oss_forbid_overwrite"]),
                "key": (None, key),
                "success_action_status": (None, "200"),
                "file": (file_name, f),
            }
            resp = requests.post(policy_data["upload_host"], files=files, timeout=self.upload_timeout_seconds)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to upload file: {resp.status_code} {resp.text}")
        logger.info("File uploaded to Bailian temp OSS, status: %s", resp.status_code)
        return f"oss://{key}"

    def _build_messages(self, prompt: str) -> list[dict]:
        """
        鎸夌櫨鐐?OpenAI compatible 鐨勬秷鎭牸寮忔瀯閫犺姹傘€?        """
        if self.input_mode == "video":
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "video_url", "video_url": {"url": self.video_url}},
                    ],
                }
            ]
        return [
            {
                "role": "user",
                "content": f"{prompt}\n\nVIDEO_URL: {self.video_url}",
            }
        ]

    def _build_text_only_messages(self, prompt: str) -> list[dict]:
        # 绾枃鏈樁娈典笉鍐嶄紶瀹屾暣瑙嗛锛屽彧娑堣垂鍓嶅簭缂撳瓨
        return [
            {
                "role": "user",
                "content": prompt,
            }
        ]

    def _build_stage2_image_messages(self, prompt: str, image_paths: List[str]) -> list[dict]:
        # 闃舵2涓撶敤娑堟伅锛氭枃鏈?+ 鍏抽敭甯у浘鐗囩粍
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image_path in image_paths:
            mime_type, _ = mimetypes.guess_type(image_path)
            mime_type = mime_type or "image/jpeg"
            with open(image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )
        return [{"role": "user", "content": content}]

    def _build_stage3_image_messages(self, prompt: str, image_paths: List[str]) -> list[dict]:
        # 阶段3与阶段2复用同样的图片组输入格式，但每次只处理单个事件
        return self._build_stage2_image_messages(prompt, image_paths)

    def _chat(self, prompt: str) -> str:
        frame = inspect.currentframe()
        try:
            caller = frame.f_back if frame else None
            if caller:
                caller_file = os.path.basename(caller.f_code.co_filename)
                caller_func = caller.f_code.co_name
                logger.info("Bailian chat call from %s:%s", caller_file, caller_func)
        finally:
            del frame
        extra_headers = None
        if isinstance(self.video_url, str) and self.video_url.startswith("oss://"):
            extra_headers = {"X-DashScope-OssResourceResolve": "enable"}
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=self._build_messages(prompt),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            extra_headers=extra_headers,
        )
        usage = getattr(resp, "usage", None)
        if usage:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
            total_tokens = getattr(usage, "total_tokens", None)
            cached_tokens = None
            prompt_details = getattr(usage, "prompt_tokens_details", None)
            if prompt_details is not None:
                cached_tokens = getattr(prompt_details, "cached_tokens", None)
            logger.info(
                "Bailian token usage: prompt=%s completion=%s total=%s cached=%s",
                prompt_tokens,
                completion_tokens,
                total_tokens,
                cached_tokens,
            )
        choice = resp.choices[0]
        finish = getattr(choice, "finish_reason", None)
        if finish:
            logger.info("Bailian finish_reason: %s", finish)
        return choice.message.content or ""

    def _chat_stage2_with_images(self, prompt: str, image_paths: List[str]) -> str:
        resp = self.client.chat.completions.create(
            model=self.stage2_model,
            messages=self._build_stage2_image_messages(prompt, image_paths),
            temperature=self.stage2_temperature,
            max_tokens=self.stage2_max_tokens,
        )
        return resp.choices[0].message.content or ""

    def _chat_stage3_with_images(self, prompt: str, image_paths: List[str]) -> str:
        resp = self.client.chat.completions.create(
            model=self.stage3_model,
            messages=self._build_stage3_image_messages(prompt, image_paths),
            temperature=self.stage3_temperature,
            max_tokens=self.stage3_max_tokens,
        )
        return resp.choices[0].message.content or ""


    def analyze_full_context(self, ctx: ActivityContext) -> Dict[str, Any]:
        """
        涓€娆℃€ц幏寰梕xtracted_context
        """
        with open(resolve_prompt_path("full_context"), "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(
                activity=ctx.activity,
                people=ctx.people,
                time=ctx.time,
                location=ctx.location,
                video_length=getattr(ctx, "video_length", "") or "",
            )

        out_dir = os.path.join(self.output_dir, "bailian")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "raw_full_context_resp.txt")

        raw = self._chat(prompt)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(raw)

        data = parse_json_from_llm(raw)
        if not isinstance(data, dict):
            return {}

        # Normalize top-level fields
        data.setdefault("activity", ctx.activity)
        data.setdefault("people", ctx.people)
        data.setdefault("time", ctx.time)
        data.setdefault("location", ctx.location)
        data.setdefault("activity_audio_clue", None)

        avc = data.get("activity_visual_clue")
        if isinstance(avc, str):
            data["activity_visual_clue"] = [avc]
        elif avc is None:
            data["activity_visual_clue"] = []

        if not isinstance(data.get("activity_visual_clue"), dict):
            clues = data.get("activity_visual_clue") or []
            data["activity_visual_clue"] = clues[0] if clues and isinstance(clues[0], dict) else {}

        if not isinstance(data.get("activity_audio_clue"), dict):
            data["activity_audio_clue"] = None

        # Normalize events: allow list -> dict
        events = data.get("events") or {}
        if isinstance(events, list):
            normalized_events = {}
            for idx, event in enumerate(events, start=1):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("id") or f"e{idx}"
                normalized_events[event_id] = event
            events = normalized_events

        if not isinstance(events, dict):
            events = {}

        for event_id, event in events.items():
            if not isinstance(event, dict):
                continue
            event.setdefault("name", "")
            event.setdefault("description", "")
            event.setdefault("start_time", "")
            event.setdefault("end_time", "")
            event.setdefault("scene_clues", [])
            event.setdefault("entities", [])
            if not isinstance(event.get("video_clip"), dict):
                event["video_clip"] = None
            event["_entity_status"] = "done"
            event["_entity_error"] = None

        data["events"] = events
        return data

    def analyze_stage1_video_parse(self, ctx: ActivityContext) -> Dict[str, Any]:
        # 闃舵1涓撶敤瑙ｆ瀽锛氬彧杈撳嚭灞€閮ㄦ椂闂磋酱鍜屽叧閿抚缁戝畾缁撴灉
        with open(resolve_prompt_path("stage1_video_parse"), "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(
                activity=ctx.activity,
                people=ctx.people,
                time=ctx.time,
                location=ctx.location,
                video_length=getattr(ctx, "video_length", "") or "",
            )

        out_dir = os.path.join(self.output_dir, "bailian")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "raw_stage1_video_parse_resp.txt")

        raw = self._chat(prompt)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(raw)

        data = parse_json_from_llm(raw)
        normalized = _normalize_stage1_video_parse(data, ctx)
        errors = validate_stage1_schema(normalized)
        if errors:
            raise ValueError(f"Invalid stage1 schema: {errors}")
        return normalized

    def analyze_stage2_event_rebuild(self, stage1_output: Dict[str, Any]) -> Dict[str, Any]:
        # 阶段2合并完成事件重建和最终实体筛选，不再拆成单独的 entity stage
        with open(resolve_prompt_path("stage2_event_rebuild"), "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(
                stage1_json=json.dumps(stage1_output, ensure_ascii=False, indent=2),
            )

        out_dir = os.path.join(self.output_dir, "bailian")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "raw_stage2_event_rebuild_resp.txt")

        image_paths = _resolve_stage1_image_paths(stage1_output, self.output_dir)
        raw = self._chat_stage2_with_images(prompt, image_paths)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(raw)

        data = parse_json_from_llm(raw)
        normalized = _normalize_stage2_event_rebuild(data, stage1_output)
        normalized = _post_filter_stage2_entities(normalized, stage1_output)
        errors = validate_stage2_schema(normalized)
        if errors:
            raise ValueError(f"Invalid stage2 schema: {errors}")
        return normalized

    def analyze_stage3_detail_generate(
        self,
        stage1_output: Dict[str, Any],
        stage2_output: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 阶段3按实体生成单点细节题，只消费阶段1证据帧和阶段2最终实体
        with open(resolve_prompt_path("stage3_detail_generate"), "r", encoding="utf-8") as f:
            template = Template(f.read())

        out_dir = os.path.join(self.output_dir, "bailian")
        os.makedirs(out_dir, exist_ok=True)

        frame_map = _build_stage1_frame_map(stage1_output)
        entity_outputs = []

        for event in stage2_output.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id", "")
            event_name = event.get("event_name", "")

            for entity in event.get("selected_entities") or []:
                if not isinstance(entity, dict):
                    continue

                entity_id = entity.get("entity_id", "")
                evidence_frame_ids = [
                    frame_id for frame_id in (entity.get("evidence_frame_ids") or [])
                    if frame_id in frame_map
                ]
                if entity.get("anchor_frame_id") and entity["anchor_frame_id"] not in evidence_frame_ids:
                    evidence_frame_ids.append(entity["anchor_frame_id"])

                image_paths: List[str] = []
                seen_paths: set[str] = set()
                for frame_id in evidence_frame_ids:
                    frame = frame_map.get(frame_id) or {}
                    image_path = frame.get("image_path")
                    if not image_path:
                        continue
                    abs_path = image_path if os.path.isabs(image_path) else os.path.join(self.output_dir, image_path)
                    abs_path = os.path.normpath(abs_path)
                    if os.path.exists(abs_path) and abs_path not in seen_paths:
                        seen_paths.add(abs_path)
                        image_paths.append(abs_path)

                entity_context = {
                    "entity_id": entity_id,
                    "entity_name": entity.get("entity_name", ""),
                    "entity_type": entity.get("entity_type", ""),
                    "selection_reason": entity.get("selection_reason", ""),
                    "evidence_frame_ids": evidence_frame_ids,
                    "anchor_frame_id": entity.get("anchor_frame_id", ""),
                    "event_id": event_id,
                    "event_name": event_name,
                }
                prompt = template.render(
                    activity_name=(stage2_output.get("metadata") or {}).get("activity_name", ""),
                    entity_json=json.dumps(entity_context, ensure_ascii=False, indent=2),
                )
                raw = self._chat_stage3_with_images(prompt, image_paths)
                raw_path = os.path.join(out_dir, f"raw_stage3_detail_generate_{entity_id}.txt")
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(raw)

                parsed = parse_json_from_llm(raw)
                entity_outputs.append(
                    {
                        "entity_id": entity_id,
                        "event_id": event_id,
                        "event_name": event_name,
                        "entity_name": entity.get("entity_name", ""),
                        "anchor_frame_id": entity.get("anchor_frame_id", ""),
                        "detail_items": (parsed or {}).get("detail_items", []),
                    }
                )

        normalized = _normalize_stage3_detail_generate(
            {"entity_details": entity_outputs},
            stage2_output,
        )
        errors = validate_stage3_schema(normalized)
        if errors:
            raise ValueError(f"Invalid stage3 schema: {errors}")
        return normalized
