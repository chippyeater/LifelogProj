import json
import os
import re
import json
import hashlib
import logging
import shutil
from typing import List, Dict, Any, Tuple, Optional

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
for name in ("huggingface", "transformers", "sentence_transformers"):
    logging.getLogger(name).setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


from dotenv import load_dotenv

# 先加载 .env，确保 HF_* 缓存配置在模型/Hub 导入前生效
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from runtime_config import (
    apply_huggingface_cache_config,
    get_config_value,
    resolve_backend_path,
    resolve_prompt_path,
)

apply_huggingface_cache_config()

import numpy as np
from sentence_transformers import SentenceTransformer
from llm_client import BailianLLM
from clue_aigc_generator import VolcEngineAIGCGenerator
from utils.media_export import (
    build_asset_relative_path,
    clamp_media_range,
    export_audio_clip,
    export_video_clip,
)

OUTPUT_DIR = resolve_backend_path(get_config_value("paths.output_root", "output"))
INPUT_PATH = os.path.join(OUTPUT_DIR, "extracted_context.json")
OUTPUT_GAME_META = os.path.join(OUTPUT_DIR, "GameMeta.json")
OUTPUT_GAME_FLOW = os.path.join(OUTPUT_DIR, "GameFlow.json")

SIMILARITY_THRESHOLD = float(get_config_value("models.embedding.event_similarity_threshold", 0.9))
EMBEDDING_MODEL_NAME = str(get_config_value("models.embedding.model_name", "moka-ai/m3e-base"))
ENTITY_SIMILARITY_THRESHOLD = float(get_config_value("models.embedding.entity_similarity_threshold", 0.9))
ACTIVITY_OPTION_TOTAL = int(get_config_value("unity.activity_option_total", 3))
ACTIVITY_POOL = ["赏花", "做家务", "玩游戏", "逛街", "运动", "看书", "看电影", "聚会", "旅行", "上班", "做饭", "遛狗", "看展览", "听音乐会", "看演唱会", "参观博物馆", "户外烧烤", "室内聚餐"]
DETAIL_OPTION_TOTAL = int(get_config_value("unity.detail_option_total", 4))


def _load_embedding_model() -> Optional[Any]:
    # 句向量模型加载失败时自动降级，避免本地内存或页面文件不足直接中断整条链路。
    try:
        return SentenceTransformer(EMBEDDING_MODEL_NAME)
    except Exception as e:
        logger.warning("Failed to load SentenceTransformer, fallback to lightweight similarity: %s", e)
        return None

def _sanitize_filename_component(text: str, max_len: int = 32) -> str:
    if not text:
        return "unknown"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or "unknown")[:max_len]


def _sanitize_human_filename_component(text: str, max_len: int = 64) -> str:
    if not text:
        return "unknown"
    cleaned = str(text).strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "unknown")[:max_len]


def _build_option_image_name(stage_id: str, option_text: str, idx: int) -> str:
    return f"{_sanitize_human_filename_component(option_text)}.jpg"


def _build_safe_media_filename(prefix: str, raw_text: str, ext: str, max_len: int = 24) -> str:
    safe_prefix = _sanitize_filename_component(prefix, max_len=16)
    safe_text = _sanitize_filename_component(raw_text, max_len=max_len)
    digest = hashlib.sha1(str(raw_text).encode("utf-8")).hexdigest()[:8]
    clean_ext = ext.lstrip(".")
    return f"{safe_prefix}_{safe_text}_{digest}.{clean_ext}"


def _copy_confusion_image_to_enhanced(
    *,
    source_path: str,
    event_name: str,
    output_dir: Optional[str],
) -> str:
    if not output_dir:
        return source_path.replace("\\", "/")
    enhanced_dir = os.path.join(output_dir, "enhanced")
    os.makedirs(enhanced_dir, exist_ok=True)
    ext = os.path.splitext(source_path)[1].lower() or ".jpg"
    dest_name = _build_safe_media_filename("confusion", event_name or "event", ext.lstrip("."))
    dest_path = os.path.join(enhanced_dir, dest_name)
    shutil.copy2(source_path, dest_path)
    return f"enhanced/{dest_name}"


_ASSET_PATH_PREFIXES = ("frames/", "enhanced/", "option_images/", "media/", "assets/")


def _iter_referenced_asset_paths(obj: Any) -> List[str]:
    paths: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
            return
        if isinstance(node, list):
            for value in node:
                _walk(value)
            return
        if not isinstance(node, str):
            return
        normalized = node.replace("\\", "/").strip()
        if normalized.startswith(("http://", "https://")):
            return
        if normalized.startswith(_ASSET_PATH_PREFIXES):
            paths.append(normalized)

    _walk(obj)
    return sorted(set(paths))


def _contains_only_ascii(path: str) -> bool:
    return all(ord(ch) < 128 for ch in path)


def validate_generated_assets(output_dir: str, game_meta: Dict[str, Any], game_flow: Dict[str, Any]) -> Dict[str, Any]:
    referenced = _iter_referenced_asset_paths(game_meta) + _iter_referenced_asset_paths(game_flow)
    unique_paths = sorted(set(referenced))
    missing: List[str] = []
    non_ascii: List[str] = []

    for asset_path in unique_paths:
        relative_path = asset_path
        if relative_path.startswith("assets/"):
            parts = relative_path.split("/", 2)
            relative_path = parts[2] if len(parts) == 3 else ""
        if relative_path and not os.path.exists(os.path.join(output_dir, relative_path)):
            missing.append(asset_path)
        if not _contains_only_ascii(asset_path):
            non_ascii.append(asset_path)

    return {
        "ok": not missing,
        "asset_count": len(unique_paths),
        "missing_assets": missing,
        "non_ascii_assets": non_ascii,
    }


def _ensure_list_unique(items: List[str]) -> List[str]:
    out = []
    for it in items:
        if it in out:
            continue
        out.append(it)
    return out


def _build_detail_options(detail_option_total: int, correct_items: List[str], distractors: List[str]) -> List[str]:
    correct_items = [c for c in (correct_items or []) if c]
    distractors = [d for d in (distractors or []) if d]
    options = _ensure_list_unique(correct_items + distractors)
    if len(options) > detail_option_total:
        options = options[:detail_option_total]
    return options


def _generate_option_images(
    *,
    options: List[str],
    stage_id: str,
    output_dir: str,
    generator: VolcEngineAIGCGenerator,
    prompt_style: str = "object",
    regenerate_assets: bool = False,
) -> List[Dict[str, str]]:
    os.makedirs(output_dir, exist_ok=True)
    results = []
    for idx, opt in enumerate(options, start=1):
        if not opt:
            continue
        filename = _build_option_image_name(stage_id, opt, idx)
        final_path = os.path.join(output_dir, filename)
        relative_path = f"option_images/{filename}"
        # 已有目标图时直接复用，避免重复生图。
        if os.path.exists(final_path) and not regenerate_assets:
            results.append({"text": opt, "image_path": relative_path})
            continue
        if prompt_style == "activity":
            prompt = (
                f"请生成一张插画风格图像，类型：场景插画素材。"
                f"目标场景是{opt}。"
                "构图为近景/特写，主体占满画面，减少留白。"
                "画面中不要出现任何文字、数字、标注或水印。"
            )
        else:
            prompt = (
                f"生成插画风格单物体素材图。主体：{opt}。"
                "近景特写，主体居中，占画面90%以上。"
                "背景必须为纯白色（#FFFFFF），单一颜色，无渐变、无阴影场景、无黑底。"
                "画面中严禁出现任何文字、字母、数字、标注、标签、水印或Logo。"
                "仅保留主体，不要其他元素。"
            )
        img_path = generator.generate_image_text(
            prompt=prompt[: int(get_config_value("aigc.prompt_max_chars", 800))],
            scale=float(get_config_value("aigc.option_image_scale", 2.5)),
            width=int(get_config_value("aigc.option_image_width", 512)),
            height=int(get_config_value("aigc.option_image_height", 512)),
        )
        if os.path.abspath(img_path) != os.path.abspath(final_path):
            os.replace(img_path, final_path)
        results.append({"text": opt, "image_path": relative_path})
    return results


def _norm_text(s: str) -> str:
    # 规范化文本：去空白/标点/大小写，用于去重比较
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。！？、,\.\!\?\-—_（）()\[\]{}<>【】]", "", s)
    return s


def _similar(a: str, b: str) -> float:
    # 计算两段文本的字符级 Jaccard 相似度
    a_n = _norm_text(a)
    b_n = _norm_text(b)
    if not a_n or not b_n:
        return 0.0
    set_a = set(a_n)
    set_b = set(b_n)
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def _select_confusion_events(user_events: List[str], confusion_count: int, material_dir: str) -> List[Dict[str, str]]:
    """
    从素材库中选择与用户事件语义相似度最低的混淆事件。
    返回格式: [{"event_name": str, "image_path": str}, ...]
    """
    if confusion_count <= 0:
        return []
    
    # 获取素材库中的所有事件图片
    if not os.path.exists(material_dir):
        logger.warning(f"Material directory {material_dir} does not exist")
        return []
    
    material_events = []
    supported_exts = {".jpg", ".jpeg", ".png"}
    for file in os.listdir(material_dir):
        ext = os.path.splitext(file)[1].lower()
        if ext in supported_exts:
            event_name = os.path.splitext(file)[0]
            image_path = os.path.join(material_dir, file)
            material_events.append({"event_name": event_name, "image_path": image_path})
    
    if not material_events:
        logger.warning(f"No supported image files found in {material_dir}")
        return []
    
    # 过滤掉与用户事件相同的
    user_event_names = set(user_events)
    filtered_materials = [m for m in material_events if m["event_name"] not in user_event_names]
    
    if not filtered_materials:
        logger.warning("No dissimilar events found in material library")
        return []
    
    # 使用语义相似度选择最不相似的事件
    try:
        model = _load_embedding_model()
        if model is not None:
            user_embeddings = model.encode(user_events, convert_to_tensor=True)
            material_embeddings = model.encode([m["event_name"] for m in filtered_materials], convert_to_tensor=True)

            # 计算每个素材事件与所有用户事件的平均相似度
            similarities = []
            for mat_emb in material_embeddings:
                sims = model.similarity(mat_emb, user_embeddings)
                avg_sim = sims.mean().item()
                similarities.append(avg_sim)
        else:
            # 轻量降级：用字符级相似度近似语义相似度，优先选最不相似的混淆事件。
            similarities = []
            for material in filtered_materials:
                event_name = material["event_name"]
                avg_sim = sum(_similar(event_name, user_event) for user_event in user_events) / max(1, len(user_events))
                similarities.append(avg_sim)

        sorted_indices = sorted(range(len(similarities)), key=lambda i: similarities[i])
        selected = [filtered_materials[i] for i in sorted_indices[:confusion_count]]
        logger.info(f"Selected {len(selected)} confusion events: {[s['event_name'] for s in selected]}")
        return selected
    except Exception as e:
        logger.warning(f"Failed to compute semantic similarity: {e}, falling back to random selection")
        import random
        selected = random.sample(filtered_materials, min(confusion_count, len(filtered_materials)))
        return selected


def _entity_fingerprint(entity: Dict[str, Any]) -> str:
    name = entity.get("item_name") or ""
    entity_clues = entity.get("entity_clues") or {}
    visual = " ".join(entity_clues.get("visual") or [])
    semantic = " ".join(entity_clues.get("semantic") or [])
    return f"{name} {semantic} {visual}"


def _dedupe_entities(
    entities: List[Dict[str, Any]],
    threshold: float,
    *,
    log_pairs: Optional[List[Dict[str, Any]]] = None,
    event_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for ent in entities or []:
        fp = _entity_fingerprint(ent)
        is_dup = False
        for k in kept:
            sim = _similar(fp, _entity_fingerprint(k))
            if sim >= threshold:
                if log_pairs is not None:
                    log_pairs.append(
                        {
                            "event": event_name or "",
                            "removed_item": ent.get("item_name") or "",
                            "kept_item": k.get("item_name") or "",
                            "similarity": sim,
                        }
                    )
                is_dup = True
                break
        if not is_dup:
            kept.append(ent)
    return kept


def _event_sort_key(e: Dict[str, Any]) -> float:
    # 解析 start_time 为秒，用于事件排序
    ts = e.get("start_time") or "00:00:00"
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600 + float(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return float(m) * 60 + float(s)
        return float(parts[0])
    except Exception:
        return 0.0


def _extract_first_json_array(text: str) -> List[str]:
    if not text:
        return []
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    snippet = text[start : end + 1]
    try:
        data = json.loads(snippet)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        return []
    return []


def _extract_first_json_array_payload(text: str) -> List[Any]:
    if not text:
        return []
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    snippet = text[start : end + 1]
    try:
        data = json.loads(snippet)
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _looks_placeholder_option(s: str) -> bool:
    if not s:
        return True
    s = s.strip()
    if re.match(r"^(干扰|错误|选项)[A-Za-z0-9]+$", s):
        return True
    if re.match(r"^[A-Da-d]$", s):
        return True
    return False


def _generate_detail_distractor_options_with_llm(
    activity: str,
    events: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    if not events:
        return {}
    model_name = str(
        get_config_value("models.unity_generation.detail_distractor_batch_model", "qwen3.6-plus")
    ).strip() or "qwen3.6-plus"
    try:
        llm = BailianLLM(model=model_name)
    except Exception as e:
        logger.warning("[LLM] Bailian init failed for detail distractors: %s", e)
        return {}

    try:
        with open(resolve_prompt_path("detail_distractor_batch"), "r", encoding="utf-8") as f:
            prompt_template = f.read()
    except Exception as e:
        logger.warning("[LLM] detail_distractor_batch prompt load failed: %s", e)
        return {}

    event_payload = []
    for event in events:
        event_name = str(event.get("event_name") or "").strip()
        correct_items = _ensure_list_unique(
            [str(item).strip() for item in (event.get("correct_items") or []) if str(item).strip()]
        )
        max_items = max(0, int(event.get("max_items") or 0))
        if not event_name or not correct_items or max_items <= 0:
            continue
        event_payload.append(
            {
                "event_name": event_name,
                "correct_items": correct_items,
                "max_items": max_items,
            }
        )
    if not event_payload:
        return {}

    prompt = prompt_template.replace("{{ACTIVITY_NAME}}", (activity or "").strip())
    prompt = prompt.replace(
        "{{EVENTS_JSON}}",
        json.dumps(event_payload, ensure_ascii=False, indent=2),
    )
    try:
        raw = llm.chat(
            messages=[
                {"role": "system", "content": "你是严格的 JSON 生成器。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=max(500, 180 * len(event_payload)),
            purpose="detail_distractor_batch",
        )
    except Exception as e:
        logger.warning("[LLM] detail distractor batch generation failed: %s", e)
        return {}

    payload = _extract_first_json_array_payload(raw)
    result: Dict[str, List[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        event_name = str(item.get("event_name") or "").strip()
        raw_options = item.get("distractor_options") or []
        if not event_name or not isinstance(raw_options, list):
            continue
        cleaned_options = []
        for option in raw_options:
            text = str(option).strip()
            if not text or _looks_placeholder_option(text):
                continue
            cleaned_options.append(text)
        if cleaned_options:
            result[event_name] = _ensure_list_unique(cleaned_options)
    return result


def _build_distractor_options(
    event_name: str,
    correct_items: List[str],
    candidate_pool: List[str],
    llm_result_map: Optional[Dict[str, List[str]]] = None,
    target_total: int = 4,
) -> List[str]:
    need = max(0, target_total - len(correct_items))
    if need == 0:
        return []
    distractors = (llm_result_map or {}).get(event_name) or []
    if distractors:
        return [d for d in distractors if d not in correct_items][:need]
    # Fallback: 使用其他事件里的物体名
    result = []
    for name in candidate_pool:
        if name in correct_items:
            continue
        if name in result:
            continue
        result.append(name)
        if len(result) >= need:
            break
    return result


def _generate_distractor_events_with_llm(activity: str, events: List[Dict[str, str]]) -> Dict[str, str]:
    if not events:
        return {}
    model_name = str(
        get_config_value("models.unity_generation.confusion_event_batch_model", "qwen3.6-plus")
    ).strip() or "qwen3.6-plus"
    try:
        llm = BailianLLM(model=model_name)
    except Exception as e:
        logger.warning("[LLM] Bailian init failed: %s", e)
        return {}

    try:
        with open(resolve_prompt_path("confusion_event_batch"), "r", encoding="utf-8") as f:
            prompt_template = f.read()
    except Exception as e:
        logger.warning("[LLM] confusion_event_batch prompt load failed: %s", e)
        return {}

    event_payload = [
        {
            "event_name": (event.get("event_name") or "").strip(),
        }
        for event in events
    ]
    prompt = prompt_template.replace("{{ACTIVITY_NAME}}", (activity or "").strip())
    prompt = prompt.replace(
        "{{EVENTS_JSON}}",
        json.dumps(event_payload, ensure_ascii=False, indent=2),
    )
    try:
        raw = llm.chat(
            messages=[
                {"role": "system", "content": "你是严格的 JSON 生成器。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=max(300, 120 * len(event_payload)),
            purpose="confusion_event_batch",
        )
    except Exception as e:
        logger.warning("[LLM] confusion event batch generation failed: %s", e)
        return {}

    payload = _extract_first_json_array_payload(raw)
    result: Dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        event_name = str(item.get("event_name") or "").strip()
        distractor = str(item.get("distractor_option") or "").strip()
        distractor = re.sub(r"^\s*混淆.*?[:：]\s*", "", distractor).strip()
        if not event_name or not distractor or _looks_placeholder_option(distractor):
            continue
        result[event_name] = distractor
    return result


def _fallback_distractor_event(event_name: str, candidate_names: List[str]) -> str:
    for candidate in candidate_names:
        if candidate and candidate != event_name:
            return candidate
    return ""


def _build_activity_options(
    activity: str,
    activity_desc: str,
    total: int,
    model: Optional[Any],
) -> List[str]:
    total = max(1, int(total or 1))
    options = [activity] if activity else []
    seen = {_norm_text(activity)} if activity else set()

    # 从ACTIVITY_POOL中选择（embedding 相似度越低越优先）
    pool = [p for p in ACTIVITY_POOL if p and _norm_text(p) not in seen]
    if pool and activity and model is not None:
        try:
            embs = model.encode([activity] + pool, normalize_embeddings=True)
            act_emb = embs[0]
            pool_embs = embs[1:]
            sims = list(np.dot(pool_embs, act_emb))
            pool = [p for _, p in sorted(zip(sims, pool), key=lambda x: x[0])]
        except Exception:
            pool = sorted(pool, key=lambda p: _similar(p, activity))
    elif pool and activity:
        pool = sorted(pool, key=lambda p: _similar(p, activity))
    for cand in pool:
        if len(options) >= total:
            break
        key = _norm_text(cand)
        if not key or key in seen:
            continue
        options.append(cand)
        seen.add(key)
    return options


def _extract_detail_question(item: Dict[str, Any]) -> Tuple[str, List[str], str]:
    detail_pair = item.get("detail_pair") or item.get("detail_pairs") or []
    if isinstance(detail_pair, dict):
        detail_pair = [detail_pair]
    if not isinstance(detail_pair, list) or not detail_pair:
        return "", [], ""
    dp0 = detail_pair[0] if isinstance(detail_pair[0], dict) else {}
    question = (dp0.get("question") or dp0.get("category") or "").strip()
    correct = (
        dp0.get("correct_value")
        or dp0.get("detail")
        or dp0.get("correct_answer")
        or ""
    )
    correct = str(correct).strip()
    options = dp0.get("confuse_options") or dp0.get("options") or []
    if not isinstance(options, list):
        options = [str(options)] if options else []
    final_opts = []
    for opt in options:
        opt = str(opt).strip()
        if not opt or opt in final_opts:
            continue
        final_opts.append(opt)
    if correct and correct not in final_opts:
        final_opts.insert(0, correct)
    if correct:
        wrong_opts = [opt for opt in final_opts if opt != correct]
        final_opts = [correct]
        if wrong_opts:
            final_opts.append(wrong_opts[0])
    else:
        final_opts = final_opts[:2]
    return question, final_opts, correct


def _is_valid_entity(entity: Dict[str, Any]) -> bool:
    # 仅在显式给出坐标且 width/height 同时为 0 时过滤实体；
    # 没有 coordinates 的旧数据仍然应保留。
    coords = entity.get("coordinates")
    if not isinstance(coords, dict) or not coords:
        return True
    try:
        w = float(coords.get("width", 0))
        h = float(coords.get("height", 0))
        if w == 0 and h == 0:
            return False
    except Exception:
        # 若坐标不可解析，保守保留
        return True
    return True


def _build_narratives(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # 基于事件列表构建 GameMeta.narratives，并映射事件字段
    narratives = []
    for idx, ev in enumerate(events, start=1):
        event_name = ev.get("name") or ev.get("description", "")
        event_desc = ev.get("description", "")
        narrative = {
            "narrative_id": f"narrative_001_{idx:03d}",
            "narrative_name": event_name,
            "narrative_description": event_desc,
            "narrative_index": idx,
        }
        # 把事件的其他字段也挂进 narrative（避免语义重复字段名）
        for k, v in ev.items():
            if k in (
                "name",
                "description",
                "entities",
                "_entity_status",
                "_entity_error",
                "__idx",
            ):
                continue
            if k in narrative:
                continue
            narrative[k] = v
        narrative["items"] = []
        narratives.append(
            narrative
        )
    return narratives


def _event_items(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 从单个事件中抽取物体/人物，items 中保留实体原始字段，并补充 item_* 字段
    if ev.get("_entity_status") != "done":
        logger.warning(
            "event entity status not done. _entity_status=%s, _entity_error=%s",
            ev.get("_entity_status"),
            ev.get("_entity_error"),
        )
    merged = []
    for it in ev.get("entities", []) or []:
        if not _is_valid_entity(it):
            continue
        name = (it.get("item_name") or "").strip()
        if not name:
            continue
        entity = dict(it)  # 保留原始字段
        entity["_item_type"] = "object"
        merged.append(entity)

    # 去重：按 item_name
    seen = set()
    filtered = []
    for it in merged:
        key = _norm_text(it.get("item_name", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        filtered.append(it)

    result = []
    for idx, it in enumerate(filtered, start=1):
        entity = dict(it)
        entity.pop("_item_type", None)
        # 移除原始 id，仅保留 item_id
        entity.pop("id", None)
        # 移除旧字段
        entity.pop("entity_images", None)
        # 先放 item_* 字段，保证顺序靠前
        item = {
            "item_id": f"item_001_{ev['__idx']:03d}_{idx:03d}",
            "item_name": entity.get("item_name", ""),
            "item_type": it["_item_type"],
            "item_index": idx,
        }
        # 追加其余原始字段
        for k, v in entity.items():
            if k in item:
                continue
            item[k] = v
        result.append(item)
    return result


def _dedup_entities_by_embedding(
    entities: List[Dict[str, Any]],
    model: Optional[Any],
    threshold: float,
) -> List[Dict[str, Any]]:
    # 基于句向量相似度去重实体（按 item_name），同类型内比较
    if not entities:
        return []
    if model is None:
        fallback_entities = []
        for entity in entities:
            clean_entity = dict(entity)
            clean_entity.pop("_entity_type", None)
            fallback_entities.append(clean_entity)
        return _dedupe_entities(fallback_entities, threshold)

    def _dedup_group(group_entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        names = [(e.get("item_name") or "").strip() for e in group_entities]
        valid = [(i, n) for i, n in enumerate(names) if n]
        if not valid:
            return []
        idxs, texts = zip(*valid)
        embs = model.encode(list(texts), normalize_embeddings=True)
        kept = []
        kept_embs = []
        for i, emb in zip(idxs, embs):
            if not kept_embs:
                kept.append(group_entities[i])
                kept_embs.append(emb)
                continue
            sims = np.dot(np.vstack(kept_embs), emb)
            if float(np.max(sims)) >= threshold:
                continue
            kept.append(group_entities[i])
            kept_embs.append(emb)
        return kept

    persons = [e for e in entities if e.get("_entity_type") == "person"]
    objects = [e for e in entities if e.get("_entity_type") == "object"]
    others = [e for e in entities if e.get("_entity_type") not in ("person", "object")]

    return _dedup_group(objects) + _dedup_group(persons) + others


def _merge_events(
    base: Dict[str, Any],
    other: Dict[str, Any],
    model: Optional[Any],
) -> None:
    # 合并事件：场景线索合并，人物/物体按句向量去重
    base_clues = base.get("scene_clues") or []
    other_clues = other.get("scene_clues") or []
    if other_clues:
        base["scene_clues"] = base_clues + other_clues

    merged = [
        m for m in ((base.get("entities") or []) + (other.get("entities") or []))
        if _is_valid_entity(m)
    ]
    for m in merged:
        m["_entity_type"] = "object"
    deduped = _dedup_entities_by_embedding(
        merged, model=model, threshold=ENTITY_SIMILARITY_THRESHOLD
    )
    for m in deduped:
        m.pop("_entity_type", None)
    base["entities"] = deduped


def _replace_text_value(obj: Any, old: str, new: str) -> Any:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            obj[k] = _replace_text_value(v, old, new)
        return obj
    if isinstance(obj, list):
        return [_replace_text_value(item, old, new) for item in obj]
    if isinstance(obj, str):
        return obj.replace(old, new)
    return obj


def _normalize_source_video(source_video: Optional[str]) -> Optional[str]:
    if not source_video:
        return None
    return os.path.normpath(source_video).replace("\\", "/")


def _build_media_url_payload(source_video: Optional[str], clip_obj: Any) -> Optional[Dict[str, Any]]:
    if not source_video or not isinstance(clip_obj, dict):
        return None
    start_time = clip_obj.get("start_time") or clip_obj.get("start")
    end_time = clip_obj.get("end_time") or clip_obj.get("end")
    if not start_time or not end_time:
        return None
    payload: Dict[str, Any] = {
        "source_video": _normalize_source_video(source_video),
        "start_time": str(start_time),
        "end_time": str(end_time),
    }
    reason = clip_obj.get("reason")
    if reason:
        payload["reason"] = str(reason)
    return payload


def _resolve_source_video_path(source_video: Optional[str]) -> Optional[str]:
    # 把上下文里的源视频路径解析成当前机器可访问的实际文件路径
    normalized = _normalize_source_video(source_video)
    if not normalized:
        return None
    if os.path.isabs(normalized) and os.path.exists(normalized):
        return normalized
    candidate = os.path.normpath(os.path.join(os.getcwd(), normalized))
    if os.path.exists(candidate):
        return candidate
    return None


def _safe_user_id(user_id: Optional[str], output_dir: Optional[str]) -> str:
    # 统一推断资源 URL 中的 user_id
    if user_id:
        return user_id
    if output_dir:
        return os.path.basename(os.path.normpath(output_dir))
    return "default"


def _export_activity_audio_asset(
    *,
    source_video_path: Optional[str],
    activity_audio_clue: Any,
    output_dir: Optional[str],
    user_id: str,
    regenerate_assets: bool = False,
) -> str:
    # 导出整体活动主题音频，并返回 assets/... 相对路径
    if not source_video_path or not isinstance(activity_audio_clue, dict) or not output_dir:
        return ""
    start_time = activity_audio_clue.get("start_time")
    end_time = activity_audio_clue.get("end_time")
    if not start_time or not end_time:
        return ""

    clipped_start, clipped_end = clamp_media_range(
        str(start_time),
        str(end_time),
        min_seconds=float(get_config_value("video_processing.media_export.activity_audio_min_seconds", 3)),
        max_seconds=float(get_config_value("video_processing.media_export.activity_audio_max_seconds", 8)),
    )
    relative_path = "media/audio/activity_theme_audio.mp3"
    output_path = os.path.join(output_dir, "media", "audio", "activity_theme_audio.mp3")
    # 已有音频片段时直接复用，除非明确要求重生成。
    if not (os.path.exists(output_path) and not regenerate_assets):
        export_audio_clip(source_video_path, clipped_start, clipped_end, output_path)
    return build_asset_relative_path(user_id, relative_path)


def _export_event_video_asset(
    *,
    source_video_path: Optional[str],
    event_id: str,
    video_clip: Any,
    output_dir: Optional[str],
    user_id: str,
    regenerate_assets: bool = False,
) -> str:
    # 导出事件视频片段，并返回 assets/... 相对路径
    if not source_video_path or not isinstance(video_clip, dict) or not output_dir or not event_id:
        return ""
    start_time = video_clip.get("start_time")
    end_time = video_clip.get("end_time")
    if not start_time or not end_time:
        return ""

    clipped_start, clipped_end = clamp_media_range(
        str(start_time),
        str(end_time),
        min_seconds=float(get_config_value("video_processing.media_export.event_video_min_seconds", 4)),
        max_seconds=float(get_config_value("video_processing.media_export.event_video_max_seconds", 10)),
    )
    filename = _build_safe_media_filename("event", event_id or "video", "mp4")
    relative_path = f"media/video/{filename}"
    output_path = os.path.join(output_dir, "media", "video", filename)
    # 已有事件视频时直接复用，除非明确要求重生成。
    if not (os.path.exists(output_path) and not regenerate_assets):
        export_video_clip(source_video_path, clipped_start, clipped_end, output_path)
    return build_asset_relative_path(user_id, relative_path)


def _reindex_game_flow_stages(game_flow: List[Dict[str, Any]]) -> None:
    # 插入混淆任务后，统一重排所有 stage 的 id 和索引，避免编号断裂或顺序错乱。
    for idx, stage in enumerate(game_flow, start=1):
        if not isinstance(stage, dict):
            continue
        stage["stage_id"] = f"stage_{idx:03d}"
        stage["stage_index"] = idx


def _get_activity_visual_clue(data: Dict[str, Any]) -> Dict[str, Any]:
    clue = data.get("activity_visual_clue")
    if isinstance(clue, dict):
        return clue
    clues = data.get("activity_visual_clues") or []
    if isinstance(clues, list) and clues and isinstance(clues[0], dict):
        return clues[0]
    return {}


def generate_game_meta_flow(
    input_path: str = INPUT_PATH,
    output_dir: Optional[str] = OUTPUT_DIR,
    confusion_count: int = 0,
    base_url: Optional[str] = None,
    user_id: Optional[str] = None,
    regenerate_assets: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    logger.info("Unity JSON generation")
    # 读取后端输出，生成 Unity 侧所需的 GameMeta/GameFlow
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input not found: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    output_meta = None
    output_flow = None
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        output_meta = os.path.join(output_dir, "GameMeta.json")
        output_flow = os.path.join(output_dir, "GameFlow.json")

    activity = data.get("activity", "")
    activity_desc = data.get("activity_description", "")
    source_video = _normalize_source_video(data.get("source_video"))
    source_video_path = _resolve_source_video_path(source_video)
    resolved_user_id = _safe_user_id(user_id, output_dir)
    activity_visual_clue = _get_activity_visual_clue(data)
    activity_audio_url = _export_activity_audio_asset(
        source_video_path=source_video_path,
        activity_audio_clue=data.get("activity_audio_clue"),
        output_dir=output_dir,
        user_id=resolved_user_id,
        regenerate_assets=regenerate_assets,
    )
    events_raw = list((data.get("events") or {}).values())
    events_raw.sort(key=_event_sort_key)

    removed_pairs = []
    removed_entity_pairs: List[Dict[str, Any]] = []
    # 统一从 extracted_context 重新生成，避免旧输出把新字段结构覆盖掉
    model = _load_embedding_model()
    names = [ev.get("name", "") for ev in events_raw]
    embeddings = model.encode(names, normalize_embeddings=True) if (names and model is not None) else np.zeros((0, 0))

    filtered = []
    kept_idx = []
    for i, ev in enumerate(events_raw):
        if not kept_idx:
            filtered.append(ev)
            kept_idx.append(i)
            continue
        last_i = kept_idx[-1]
        sim = float(np.dot(embeddings[i], embeddings[last_i])) if model is not None else _similar(names[i], names[last_i])
        if sim >= SIMILARITY_THRESHOLD:
            _merge_events(filtered[-1], ev, model)
            removed_pairs.append(
                {
                    "removed_index": i,
                    "removed_event": names[i],
                    "kept_index": last_i,
                    "kept_event": names[last_i],
                    "similarity": sim,
                }
            )
            continue
        filtered.append(ev)
        kept_idx.append(i)

    for idx, ev in enumerate(filtered, start=1):
        ev["__idx"] = idx

    for ev in filtered:
        if isinstance(ev, dict):
            ev_name = ev.get("name") or ev.get("description", "")
            ev["entities"] = _dedupe_entities(
                ev.get("entities", []),
                ENTITY_SIMILARITY_THRESHOLD,
                log_pairs=removed_entity_pairs,
                event_name=ev_name,
            )

    narratives = _build_narratives(filtered)
    for i, ev in enumerate(filtered):
        narratives[i]["items"] = _event_items(ev)

    structure = {
        "structure_id": "structure_001",
        "structure_name": activity,
        "structure_index": 1,
        "people": data.get("people", ""),
        "time": data.get("time", ""),
        "location": data.get("location", ""),
        "activity_description": activity_desc,
        "hint_scene_images": activity_visual_clue,
        "narratives": narratives,
    }

    game_meta = {"game_levels": [structure]}
    game_flow = []
    existing_stage_ids: set[str] = set()

    # 收集用户事件名称，用于选择混淆事件
    user_event_names = [ev.get("name") or ev.get("description", "") for ev in filtered]
    
    # 选择混淆事件
    material_root = resolve_backend_path(get_config_value("paths.material_library_root", "output"))
    material_subdir = str(get_config_value("unity.confusion_material_subdir", "default"))
    material_dir = os.path.join(material_root, material_subdir)
    confusion_events = _select_confusion_events(user_event_names, confusion_count, material_dir)

    def _flush_partial() -> None:
        if output_meta:
            with open(output_meta, "w", encoding="utf-8") as f:
                json.dump(game_meta, f, ensure_ascii=False, indent=2)
        if output_flow:
            with open(output_flow, "w", encoding="utf-8") as f:
                json.dump({"game_flow": game_flow}, f, ensure_ascii=False, indent=2)

    volc_cfg = get_config_value("models.volcengine", {})
    aigc_cfg = get_config_value("aigc", {})
    # Config-managed: Volc credentials, option image generation knobs, and confusion material directory.
    option_image_generator = VolcEngineAIGCGenerator(
        access_key=volc_cfg.get("access_key"),
        secret_key=volc_cfg.get("secret_key"),
        output_dir=os.path.join(output_dir, "option_images"),
        add_logo=bool(aigc_cfg.get("add_logo", False)),
        add_aigc_meta=bool(aigc_cfg.get("add_aigc_meta", True)),
    )

    model = _load_embedding_model()
    if "stage_001" not in existing_stage_ids:
        stage1_options = _build_activity_options(activity, activity_desc, ACTIVITY_OPTION_TOTAL, model)
        stage1_options_with_images = None
        if option_image_generator and stage1_options:
            try:
                stage1_options_with_images = _generate_option_images(
                    options=stage1_options,
                    stage_id="stage_001",
                    output_dir=os.path.join(output_dir, "option_images"),
                    generator=option_image_generator,
                    prompt_style="activity",
                    regenerate_assets=regenerate_assets,
                )
            except Exception as e:
                logger.warning("[AIGC] stage_1 option image generation failed: %s", e)
                stage1_options_with_images = None
        
        game_flow.append(
            {
                "stage_id": "stage_001",
                "stage_name": activity,
                "stage_type": "structure",
                "stage_index": 1,
                "task_type": "selection",
                "task_description": "从多个选项中选择在" + data.get("time", "") + "内进行的活动场景主题",
                "time_info": data.get("time", ""),
                "correct_answer": activity,
                "options": stage1_options_with_images
                if stage1_options_with_images is not None
                else [{"text": opt, "image_path": None} for opt in stage1_options],
                "hint_scene_images": {
                    "puzzle_main": activity_visual_clue.get("reference_frame_path", ""),
                    "description": activity_visual_clue.get("description", ""),
                },
                "hints": {
                    "location_info": data.get("location", ""),
                    "audio_url": activity_audio_url,
                },
            }
        )
        existing_stage_ids.add("stage_001")
        _flush_partial()

    # 收集 recall 任务
    recall_tasks = []
    distractor_event_map = _generate_distractor_events_with_llm(
        activity,
        [
            {
                "event_name": ev.get("name") or ev.get("description", ""),
            }
            for ev in filtered
        ]
    )
    event_name_pool = [ev.get("name") or ev.get("description", "") for ev in filtered]
    for i, ev in enumerate(filtered, start=2):
        stage_id = f"stage_{i:03d}"
        if stage_id in existing_stage_ids:
            continue
        event_name = ev.get("name") or ev.get("description", "")
        event_desc = ev.get("description", "")
        scene_clues = ev.get("scene_clues") or []
        long_desc = event_desc or "".join(
            [c.get("description", "") for c in scene_clues if c.get("description")]
        )
        distractor_opt = distractor_event_map.get(event_name) or _fallback_distractor_event(event_name, event_name_pool)
        
        task = {
            "stage_id": stage_id,
            "stage_name": event_name,
            "stage_type": "narrative",
            "stage_index": i,
            "task_type": "recall",
            "task_description": f"回忆是否有'{event_name}'这个环节",
            "enhanced_image_path": scene_clues[0].get("enhanced_image_path", "") if scene_clues else "",
            "reference_frame_path": scene_clues[0].get("reference_frame_path", "") if scene_clues else "",
            "long_description": long_desc,
            "distractor_option": distractor_opt,
            "video_clip_url": _export_event_video_asset(
                source_video_path=source_video_path,
                event_id=ev.get("id") or event_name or stage_id,
                video_clip=ev.get("video_clip"),
                output_dir=output_dir,
                user_id=resolved_user_id,
                regenerate_assets=regenerate_assets,
            ),
            "correct_answer": True,
        }
        recall_tasks.append(task)
        existing_stage_ids.add(stage_id)
    
    # 随机插入混淆任务
    import random
    for conf in confusion_events:
        insert_pos = random.randint(0, len(recall_tasks))
        copied_image_path = _copy_confusion_image_to_enhanced(
            source_path=conf["image_path"],
            event_name=conf["event_name"],
            output_dir=output_dir,
        )
        conf_task = {
            "stage_id": "",
            "stage_name": conf["event_name"],
            "stage_type": "narrative",
            "stage_index": 0,
            "task_type": "recall",
            "task_description": f"回忆是否有'{conf['event_name']}'这个环节",
            "enhanced_image_path": copied_image_path,
            "reference_frame_path": copied_image_path,
            "correct_answer": False,
        }
        recall_tasks.insert(insert_pos, conf_task)
    
    # 添加 recall 任务到 game_flow，并调整 stage_index
    for task in recall_tasks:
        game_flow.append(task)

    _reindex_game_flow_stages(game_flow)
    for stage in game_flow:
        existing_stage_ids.add(stage.get("stage_id", ""))
    _flush_partial()

    seq_start = len(game_flow) + 1
    total_items = len(filtered)
    for i, ev in enumerate(filtered, start=0):
        stage_id = f"stage_{seq_start + i:03d}"
        if stage_id in existing_stage_ids:
            continue
        event_name = ev.get("name") or ev.get("description", "")
        game_flow.append(
            {
                "stage_id": stage_id,
                "stage_name": event_name,
                "stage_type": "narrative",
                "stage_index": seq_start + i,
                "task_type": "sequencing",
                "task_description": f"将'{event_name}'放在正确的顺序位置",
                "correct_answer": event_name,
                "correct_position": i + 1,
                "total_items": total_items,
            }
        )
        existing_stage_ids.add(stage_id)
        _flush_partial()

    detail_start = 2 + len(recall_tasks) + len(filtered)
    all_items_pool = []
    for ev2 in filtered:
        ev_items = _event_items(ev2)
        all_items_pool.extend([it.get("item_name") for it in ev_items if it.get("item_name")])
    detail_distractor_map = _generate_detail_distractor_options_with_llm(
        activity,
        [
            {
                "event_name": ev.get("name") or ev.get("description", ""),
                "correct_items": [it.get("item_name") for it in _event_items(ev) if it.get("item_name")],
                "max_items": max(
                    1,
                    DETAIL_OPTION_TOTAL
                    - len([it.get("item_name") for it in _event_items(ev) if it.get("item_name")]),
                ),
            }
            for ev in filtered
            if _event_items(ev)
        ],
    )

    for i, ev in enumerate(filtered, start=0):
        stage_id = f"stage_{detail_start + i:03d}"
        if stage_id in existing_stage_ids:
            continue
        event_name = ev.get("name") or ev.get("description", "")
        event_items = _event_items(ev)
        items = [it.get("item_name") for it in event_items if it.get("item_name")]
        distractor_options = _build_distractor_options(
            event_name,
            items,
            all_items_pool,
            llm_result_map=detail_distractor_map,
            target_total=DETAIL_OPTION_TOTAL,
        )
        options_text = _build_detail_options(DETAIL_OPTION_TOTAL, items, distractor_options)
        options_with_images = None
        if option_image_generator and options_text:
            try:
                options_with_images = _generate_option_images(
                    options=options_text,
                    stage_id=f"stage_{detail_start + i:03d}",
                    output_dir=os.path.join(output_dir, "option_images"),
                    generator=option_image_generator,
                    regenerate_assets=regenerate_assets,
                )
            except Exception as e:
                logger.warning("[AIGC] option image generation failed: %s", e)
                options_with_images = None
        
        game_flow.append(
            {
                "stage_id": stage_id,
                "stage_name": event_name,
                "stage_type": "item",
                "stage_index": detail_start + i,
                "task_type": "detail_recall",
                "task_description": "回忆该场景中的具体物品",
                "related_narrative": event_name,
                "correct_answer": items,
                "options": options_with_images
                if options_with_images is not None
                else [{"text": opt, "image_path": None} for opt in options_text],
                "advanced_recall_questions": [
                    {
                        "target_item": it.get("item_name", ""),
                        "question": _extract_detail_question(it)[0],
                        "options": _extract_detail_question(it)[1],
                        "correct_answer": _extract_detail_question(it)[2],
                    }
                    for it in event_items
                ],
            }
        )
        existing_stage_ids.add(stage_id)
        _flush_partial()

    game_flow_obj = {"game_flow": game_flow}

    _replace_text_value(game_flow_obj, "用户", "我们")

    if output_meta:
        with open(output_meta, "w", encoding="utf-8") as f:
            json.dump(game_meta, f, ensure_ascii=False, indent=2)

    if output_flow:
        with open(output_flow, "w", encoding="utf-8") as f:
            json.dump(game_flow_obj, f, ensure_ascii=False, indent=2)

        if removed_pairs:
            logger.info("Removed similar events (threshold %.2f):", SIMILARITY_THRESHOLD)
            for p in removed_pairs:
                logger.info(
                    "- drop[%s]: %s  |  keep[%s]: %s  |  sim=%.3f",
                    p["removed_index"],
                    p["removed_event"],
                    p["kept_index"],
                    p["kept_event"],
                    p["similarity"],
                )
        else:
            logger.info("Removed similar events: none")

        if removed_entity_pairs:
            logger.info("Removed similar entities (threshold %.2f):", ENTITY_SIMILARITY_THRESHOLD)
            for p in removed_entity_pairs:
                logger.info(
                    "- event[%s]: drop[%s]  |  keep[%s]  |  sim=%.3f",
                    p["event"],
                    p["removed_item"],
                    p["kept_item"],
                    p["similarity"],
                )
        else:
            logger.info("Removed similar entities: none")

        logger.info("Generated json saved to: %s and %s", output_meta, output_flow)

    return game_meta, game_flow_obj



"""
Note: The executable entrypoint has been moved to run_pipeline.py.
This module now only exposes reusable functions.
"""

