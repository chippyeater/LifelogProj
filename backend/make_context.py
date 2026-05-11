# Python 3.12.4
"""
主流水线脚本
- 连接 TwelveLabs（video/LLM 服务）
- 对整段视频做事件切分
- 对每个事件做实体提取
- 对实体的参考帧做AIGC 线索图增强
"""
import logging
import os
import re
import hashlib
from dotenv import load_dotenv
from typing import Any, Dict, Optional
from dataclasses import asdict
import json
import cv2

# from twelvelabs import IndexesCreateRequestModelsItem, TwelveLabs

from clue_aigc_generator import VolcEngineAIGCGenerator
from my_basics import ActivityContext, parse_json_from_llm
from bailian_video_processor import BailianVideoProcessor
from utils.frame_utils import extract_frame_to_path, timestamp_to_frame_index


# 配置日志，用于调试和问题定位
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def _normalize_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return path
    return os.path.normpath(path).replace("\\", "/")

def _sanitize_filename_component(text: Optional[str], max_len: int = 24) -> str:
    if not text:
        return "unknown"
    cleaned = str(text).strip()
    # Preserve readable Unicode names; only strip characters that are invalid in Windows filenames.
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    return (cleaned or "unknown")[:max_len]

def _to_relative_asset_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return path
    norm = _normalize_path(path)
    filename = os.path.basename(norm)
    if "/frames/" in norm:
        return f"frames/{filename}"
    if "/enhanced/" in norm:
        return f"enhanced/{filename}"
    if "/option_images/" in norm:
        return f"option_images/{filename}"
    return filename

def _resolve_asset_path(path: Optional[str], root_dir: str) -> Optional[str]:
    if not path:
        return path
    norm = _normalize_path(path)
    if os.path.isabs(norm):
        return norm
    if norm.startswith("frames/"):
        return os.path.join(root_dir, "frames", os.path.basename(norm))
    if norm.startswith("enhanced/"):
        return os.path.join(root_dir, "enhanced", os.path.basename(norm))
    if norm.startswith("option_images/"):
        return os.path.join(root_dir, "option_images", os.path.basename(norm))
    return os.path.join(root_dir, norm)

def _norm_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。！？、,\.!\?;:：\\-—~·()\[\]{}<>“”\"'‘’]", "", s)
    return s


def _similar(a: str, b: str) -> float:
    a_n = _norm_text(a)
    b_n = _norm_text(b)
    if not a_n or not b_n:
        return 0.0
    set_a = set(a_n)
    set_b = set(b_n)
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def _entity_fingerprint(entity: dict) -> str:
    name = entity.get("item_name") or ""
    entity_clues = entity.get("entity_clues") or {}
    visual = " ".join(entity_clues.get("visual") or [])
    semantic = " ".join(entity_clues.get("semantic") or [])
    return f"{name} {semantic} {visual}"


def _dedupe_entities(entities: list[dict], threshold: float = 0.9) -> list[dict]:
    kept: list[dict] = []
    for ent in entities or []:
        fp = _entity_fingerprint(ent)
        is_dup = False
        for k in kept:
            if _similar(fp, _entity_fingerprint(k)) >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(ent)
    return kept


def _build_raw_filename(name: Optional[str]) -> str:
    name_part = _sanitize_filename_component(name)
    digest = hashlib.sha1(str(name or "unknown").encode("utf-8")).hexdigest()[:8]
    return f"{name_part}_{digest}.jpg"


def _build_enhanced_filename(name: Optional[str]) -> str:
    name_part = _sanitize_filename_component(name)
    digest = hashlib.sha1(str(name or "unknown").encode("utf-8")).hexdigest()[:8]
    return f"{name_part}_{digest}.jpg"


def _build_enhanced_scene_filename(
    activity_name: Optional[str], event_id: Optional[str], clue_index: int, total_clues: int
) -> str:
    event_part = _sanitize_filename_component(event_id)
    digest = hashlib.sha1(str(event_id or "unknown").encode("utf-8")).hexdigest()[:8]
    return f"{event_part}_{digest}.jpg"


def _rename_image(src_path: str, new_filename: str) -> str:
    if not src_path:
        return src_path
    dst_path = os.path.join(os.path.dirname(src_path), new_filename)
    if os.path.abspath(src_path) != os.path.abspath(dst_path):
        os.replace(src_path, dst_path)
    return _to_relative_asset_path(dst_path)


# def create_index(...):
#     TwelveLabs index creation removed for Bailian-only flow.


def execute_video_processor_single_call(
    context: ActivityContext,
    processor: BailianVideoProcessor,
    checkpoint_path: str,
) -> dict:
    """
    一次性生成extracted_context
    """
    os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)

    def _save_checkpoint(obj: dict):
        tmp = checkpoint_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            os.replace(tmp, checkpoint_path)
            logger.debug(f"Checkpoint saved to {checkpoint_path}")
        except PermissionError as e:
            logger.error(f"Permission denied when saving checkpoint: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            raise

    full_context = processor.analyze_full_context(context)
    if not full_context:
        logger.warning("No context extracted in single-call mode.")
        return {}

    _save_checkpoint(full_context)
    logger.info(f"Extracted context saved to {checkpoint_path} (single-call mode)")
    return full_context


def extract_reference_frames(full_context: dict, video_path: str, output_dir: str):
    """为每个实体/场景线索的关键帧抽帧并写入 reference_frame_path"""
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    os.makedirs(output_dir, exist_ok=True)
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open video file: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        cap.release()
        raise ValueError(f"Invalid FPS value ({fps}) for video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    activity_name = full_context.get("activity", "")

    # 处理活动层视觉线索
    activity_clue = full_context.get("activity_visual_clue") or []
    if isinstance(activity_clue, dict):
        _extract_reference_frame(
            item=activity_clue,
            item_type="activity",
            output_dir=output_dir,
            event_id=activity_name or "activity",
            item_id="",
            item_label=activity_name or "activity",
            cap=cap,
            fps=fps,
            total_frames=total_frames,
        )
    
    # 处理子事件视觉线索
    for event_id, event in full_context["events"].items():
        scene_clues = event.get("scene_clues") or []
        event_name = event.get("name") or event.get("description") or event_id
        for idx, clue in enumerate(scene_clues):
            _extract_reference_frame(
                item=clue,
                item_type="event",
                output_dir=output_dir,
                event_id=event_name,
                item_id="",
                item_label=event_name,
                cap=cap,
                fps=fps,
                total_frames=total_frames,
            )
        entities = event.get("entities", [])
        if not isinstance(entities, list):
            continue
        for entity in entities:
            entity_name = entity.get("item_name") or entity.get("id") or "entity"
            _extract_reference_frame(
                item=entity,
                item_type="entity",
                output_dir=output_dir,
                event_id=entity_name,
                item_id="",
                item_label=entity_name,
                cap=cap,
                fps=fps,
                total_frames=total_frames,
            )
    cap.release()


def _extract_reference_frame(
    *,
    item: dict,
    item_type: str,
    output_dir: str,
    event_id: Optional[str],
    item_id: str,
    item_label: str,
    cap: cv2.VideoCapture,
    fps: float,
    total_frames: int,
) -> None:
    """为任意对象抽取关键帧并写入 reference_frame_path（复用同一个 cap）"""
    key_time = item.get("frame")
    if not key_time:
        item["reference_frame_path"] = None
        return

    root_dir = os.path.dirname(output_dir)
    existing_path = _resolve_asset_path(item.get("reference_frame_path"), root_dir)
    if existing_path and os.path.exists(existing_path):
        item["reference_frame_path"] = _to_relative_asset_path(existing_path)
        logger.info("FrameExtract(%s): reuse existing frame %s", item_type, existing_path)
        return

    try:
        frame_idx = timestamp_to_frame_index(key_time, fps=fps)
        logger.info(
            "FrameExtract(%s): event=%s item=%s frame=%s fps=%.3f => frame_idx=%s total_frames=%s",
            item_type,
            event_id,
            item_label,
            key_time,
            fps,
            frame_idx,
            total_frames,
        )
        if frame_idx < 0 or frame_idx >= total_frames:
            logger.warning("Frame index %s out of range (total: %s), skipping", frame_idx, total_frames)
            item["reference_frame_path"] = None
            return

        filename = _build_raw_filename(item_label or event_id or item_id)
        img_path = os.path.join(output_dir, filename)
        if os.path.exists(img_path):
            item["reference_frame_path"] = _to_relative_asset_path(img_path)
            logger.info("FrameExtract(%s): target frame already exists %s", item_type, img_path)
            return
        ok = extract_frame_to_path(
            video_path="__reuse_existing_capture__",
            frame_time=key_time,
            output_path=img_path,
            cap=cap,
            fps=fps,
            total_frames=total_frames,
        )
        if ok:
            item["reference_frame_path"] = _to_relative_asset_path(img_path)
            logger.debug("Extracted frame for %s to %s", item_type, img_path)
        else:
            logger.warning("Failed to read frame %s for %s", frame_idx, item_type)
            item["reference_frame_path"] = None
    except Exception as e:
        logger.error("Failed to extract frame for %s: %s", item_type, e)
        item["reference_frame_path"] = None


def _extract_reference_frame_single(
    *,
    item: dict,
    item_type: str,
    video_path: str,
    output_dir: str,
    event_id: Optional[str],
    item_id: str,
    item_label: str,
) -> None:
    if not video_path or not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")
    key_time = item.get("frame")
    if not key_time:
        item["reference_frame_path"] = None
        return
    os.makedirs(output_dir, exist_ok=True)
    root_dir = os.path.dirname(output_dir)
    existing_path = _resolve_asset_path(item.get("reference_frame_path"), root_dir)
    if existing_path and os.path.exists(existing_path):
        item["reference_frame_path"] = _to_relative_asset_path(existing_path)
        return
    filename = _build_raw_filename(item_label or event_id or item_id)
    img_path = os.path.join(output_dir, filename)
    if os.path.exists(img_path):
        item["reference_frame_path"] = _to_relative_asset_path(img_path)
        return
    try:
        ok = extract_frame_to_path(video_path, key_time, img_path)
    except Exception as e:
        logger.error("Failed to extract frame for %s: %s", item_type, e)
        item["reference_frame_path"] = None
        return
    item["reference_frame_path"] = _to_relative_asset_path(img_path) if ok else None


def execute_entity_clue_enhancement(
    activity_context: Dict[str, Any],
    generator,
    video_path: str,
    checkpoint_path: str,
    frames_dir: Optional[str] = None,
    force: bool = False,
) -> None:
    """
    对 activity_context 中每个事件的 entities 执行 AIGC 增强，
    并将生成的图像路径写入 entity["enhanced_image_path"]。
    """
    os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)

    def _save_checkpoint(obj: dict):
        tmp = checkpoint_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, checkpoint_path)

    activity_desc = activity_context.get("activity", "日常活动")
    root_dir = os.path.dirname(checkpoint_path)

    for event_id, event in activity_context["events"].items():
        entities = event.get("entities", [])
        if not isinstance(entities, list):
            continue

        for entity in entities:
            entity_id = entity.get("id")
            if not entity_id:
                logger.warning(f"Entity in event {event_id} has no ID, skipping AIGC")
                entity["enhanced_image_path"] = None
                _save_checkpoint(activity_context)
                continue

            existing_image = entity.get("enhanced_image_path")
            if existing_image and not force:
                logger.info(f"AIGC already exists for {entity.get('item_name')} (ID: {entity_id}), skipping.")
                continue

            ref_frame_path = entity.get("reference_frame_path")
            ref_frame_abs = _resolve_asset_path(ref_frame_path, root_dir)
            if not ref_frame_abs or not os.path.exists(ref_frame_abs):
                logger.warning(
                    f"Reference frame missing for {entity.get('item_name')}, re-extracting this entity only."
                )
                try:
                    entity_id = entity.get("id") or "entity"
                    # 打开视频并为本实体单独抽帧
                    _extract_reference_frame_single(
                        item=entity,
                        item_type="entity",
                        video_path=video_path,
                        output_dir=frames_dir or "output/frames",
                        event_id=event_id,
                        item_id=entity_id,
                        item_label=entity.get("item_name") or entity_id,
                    )
                except Exception as e:
                    logger.error(f"Failed to re-extract frame for entity {entity_id}: {e}")

            ref_frame_path = entity.get("reference_frame_path")
            ref_frame_abs = _resolve_asset_path(ref_frame_path, root_dir)
            if not ref_frame_abs or not os.path.exists(ref_frame_abs):
                logger.warning(f"Reference frame still missing for {entity.get('item_name')}, skipping AIGC.")
                entity["enhanced_image_path"] = None
                _save_checkpoint(activity_context)
                continue

            # 构建 prompt
            item_name = entity.get("item_name", "未知实体")
            entity_type = "物品"
            event_name = event.get("name") or event.get("description", "")

            prompt_parts = [
                f"活动背景：{activity_desc}。",
                f"当前场景：{event_name}。",
                "请将输入图像转换为手绘插画风格，突出显示该目标对象，"
                "保留关键轮廓与特征，背景适度简化并虚化，"
                "整体色调柔和，增强视觉记忆线索。"
                "画面中不要出现任何文字、数字、标注或水印；"
                "主体尽量占满画面，贴近取景，减少留白。"
            ]
            prompt = "".join(prompt_parts)

            try:
                enhanced_img = generator.generate_image(
                    prompt=prompt[:800],
                    input_image_path=ref_frame_abs,
                    scale=0.65,
                )
                renamed = _rename_image(
                    enhanced_img,
                    _build_enhanced_filename(item_name),
                )
                entity["enhanced_image_path"] = renamed
                logger.info(f"AIGC enhanced for {item_name} (ID: {entity_id}): {enhanced_img}")
                _save_checkpoint(activity_context)
            except Exception as e:
                entity["enhanced_image_path"] = None
                logger.error(f"Failed to enhance {item_name} (ID: {entity_id}): {e}")
                _save_checkpoint(activity_context)
                continue


def execute_scene_clue_enhancement(
    activity_context: Dict[str, Any],
    generator,
    checkpoint_path: str,
    force: bool = False,
) -> None:
    """对 scene_clues 执行 AIGC 增强，并写入 enhanced_image_path"""
    os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)

    def _save_checkpoint(obj: dict):
        tmp = checkpoint_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, checkpoint_path)

    activity_desc = activity_context.get("activity", "日常活动")
    activity_long_desc = activity_context.get("activity_description") or activity_desc
    root_dir = os.path.dirname(checkpoint_path)

    # # 生成整体活动视觉线索增强图片
    # activity_clues = activity_context.get("activity_visual_clue") or []
    # if isinstance(activity_clues, list):
    #     for idx, clue in enumerate(activity_clues):
    #         if clue.get("enhanced_image_path") and not force:
    #             continue

    #         ref_frame_path = clue.get("reference_frame_path")
    #         if not ref_frame_path or not os.path.exists(ref_frame_path):
    #             clue["enhanced_image_path"] = None
    #             _save_checkpoint(activity_context)
    #             continue

    #         clue_desc = clue.get("description", "场景线索")
    #         prompt_parts = [
    #             f"活动背景：{activity_desc}。",
    #             f"当前场景：{activity_long_desc}。",
    #             f"目标对象：{clue_desc}。",
    #             "请将输入图像转换为手绘插画风格，突出显示该场景线索，"
    #             "保留关键轮廓与特征，背景适度简化并虚化，"
    #             "整体色调柔和，增强视觉记忆线索，但不得添加任何文字或水印。",
    #         ]
    #         prompt = "".join(prompt_parts)

    #         try:
    #             enhanced_img = generator.generate_image(
    #                 prompt=prompt[:800],
    #                 input_image_path=ref_frame_path,
    #                 scale=0.65,
    #             )
    #             renamed = _rename_image(
    #                 enhanced_img,
    #                 _build_enhanced_scene_filename(activity_desc, "activity", idx, len(activity_clues)),
    #             )
    #             clue["enhanced_image_path"] = renamed
    #             _save_checkpoint(activity_context)
    #         except Exception as e:
    #             clue["enhanced_image_path"] = None
    #             logger.error(f"Failed to enhance activity scene clue: {e}")
    #             _save_checkpoint(activity_context)
    #             continue

    # 生成子事件视觉线索增强图片
    for event_id, event in activity_context["events"].items():
        scene_clues = event.get("scene_clues") or []
        if not isinstance(scene_clues, list):
            continue
        
        for idx, clue in enumerate(scene_clues):
            if clue.get("enhanced_image_path") and not force:
                continue

            ref_frame_path = clue.get("reference_frame_path")
            ref_frame_abs = _resolve_asset_path(ref_frame_path, root_dir)
            if not ref_frame_abs or not os.path.exists(ref_frame_abs):
                clue["enhanced_image_path"] = None
                _save_checkpoint(activity_context)
                continue

            event_desc = event.get("description", "")
            event_name = event.get("name") or event_desc
            prompt_parts = [
                f"活动背景：{activity_desc}。",
                f"当前场景：{event_desc}。",
                f"目标对象：{event_name}。",
                "请将输入图像转换为手绘插画风格，突出显示该场景线索，"
                "保留关键轮廓与特征，背景适度简化并虚化，"
                "整体色调柔和，增强视觉记忆线索。"
                "画面中不要出现任何文字、数字、标注或水印；"
                "主体尽量占满画面，贴近取景，减少留白。"
            ]
            prompt = "".join(prompt_parts)

            try:
                enhanced_img = generator.generate_image(
                    prompt=prompt[:800],
                    input_image_path=ref_frame_abs,
                    scale=0.65,
                )
                renamed = _rename_image(
                    enhanced_img,
                    _build_enhanced_filename(event_name),
                )
                clue["enhanced_image_path"] = renamed
                _save_checkpoint(activity_context)
            except Exception as e:
                clue["enhanced_image_path"] = None
                logger.error(f"Failed to enhance scene clue for event {event_id}: {e}")
                _save_checkpoint(activity_context)
                continue




"""
Note: The executable entrypoint has been moved to run_pipeline.py.
This module now only exposes reusable functions.
"""




