# .venv/scripts/python.exe, Python 3.12.4
import os
from dotenv import load_dotenv
from typing import Any, Dict, Optional
from dataclasses import asdict
import json
import cv2

from twelvelabs import IndexesCreateRequestModelsItem, TwelveLabs

from clue_aigc_generator import VolcEngineAIGCGenerator
from my_basics import ActivityContext, EntityContext
from video_processor import VideoProcessor


def create_index(
    client,
    index_name: str,
    model_name: str = "Pegasus1.2",
    model_options: list[str] | None = None,
) -> str:
    if model_options is None:
        model_options = ["visual", "audio"]
    try:
        index = client.indexes.create(
            index_name=index_name,
            models=[
                IndexesCreateRequestModelsItem(
                    model_name=model_name,
                    model_options=model_options,
                ),
            ],
        )
        if index.id:
            print(f"✅ Created index: {index.id}")
            return index.id
        else:
            raise RuntimeError("Index creation returned empty ID")
    except Exception as e:
        print(f"❌ Error creating index: {e}")
        raise


def execute_video_processor(context: ActivityContext, processor: VideoProcessor) -> dict:
    """
    执行视频分析：先分事件，再对每个事件提取实体。
    返回完整的 activity_context_dict（可用于后续 AIGC 或保存）。
    """
    # Step 1: 分割事件
    event_parsed = processor.analyze_events(context)
    if not event_parsed or "events" not in event_parsed:
        print("⚠️ No events extracted.")
        return {}

    # 构建完整的上下文结构
    full_context = {
        "activity": context.activity,
        "people": context.people,
        "time": context.time,
        "location": context.location,
        "activity_description": event_parsed.get("activity_description", ""),
        "events": {},
    }

    # Step 2: 对每个事件提取实体
    for event in event_parsed["events"]:
        event_id = event["id"]
        event_desc = event["description"]
        start_ts = event["start_time"]
        end_ts = event["end_time"]

        print(f"\n🔍 Analyzing entities for event: {event_desc}")

        # 创建 EntityContext（用于 prompt 渲染）
        entity_ctx = EntityContext(
            activity=context.activity,
            people=context.people,
            time=context.time,
            sub_event=event_desc,
            start_time=start_ts,
            end_time=end_ts,
        )

        # 调用实体提取
        entity_parsed = processor.analyze_entities(
            ctx=entity_ctx,
            event_id=event_id,
            event_description=event_desc,
            start_ts=start_ts,
            end_ts=end_ts,
        )

        # 合并到 full_context
        full_context["events"][event_id] = {
            "description": event_desc,
            "start_time": start_ts,
            "end_time": end_ts,
            "scene_clues": event.get("scene_clues", []),
            **entity_parsed,  # 包含 key_persons 和 key_objects
        }

    # 保存中间结果（可选）
    with open("output/extracted_context.json", "w", encoding="utf-8") as f:
        json.dump(full_context, f, ensure_ascii=False, indent=2)
    print("✅ Extracted context saved to output/extracted_context.json")

    return full_context


def extract_reference_frames(full_context: dict, video_path: str, output_dir: str = "output/frames"):
    """为每个实体的 key_frame 时间戳抽帧，并写入 reference_frame_path"""
    os.makedirs(output_dir, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)

    def time_to_frame(time_str: str) -> int:
        h, m, s = map(float, time_str.split(":"))
        return int((h * 3600 + m * 60 + s) * fps)

    for event in full_context["events"].values():
        for group in ["key_persons", "key_objects"]:
            for entity in event.get(group, []):
                key_time = entity.get("key_frame")
                if not key_time:
                    continue
                frame_idx = time_to_frame(key_time)
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if ret:
                    img_path = os.path.join(output_dir, f"{entity['id']}.jpg")
                    cv2.imwrite(img_path, frame)
                    entity["reference_frame_path"] = img_path
                else:
                    entity["reference_frame_path"] = None
    cap.release()


def execute_aigc_enhancement(activity_context: Dict[str, Any], generator) -> None:
    """
    对 activity_context 中每个事件的 key_persons / key_objects 执行 AIGC 增强，
    并将生成的图像路径写入 entity["entity_images"]。
    """
    activity_desc = activity_context.get("activity", "日常活动")

    for event_id, event in activity_context["events"].items():
        for group in ["key_persons", "key_objects"]:
            entities = event.get(group, [])
            if not isinstance(entities, list):
                continue  # 兼容空对象 {} 的情况

            for entity in entities:
                # 获取参考帧路径（假设之前已通过视频抽帧保存）
                ref_frame_path = entity.get("reference_frame_path")
                if not ref_frame_path or not os.path.exists(ref_frame_path):
                    print(f"Reference frame missing for {entity.get('item_name')}, skipping AIGC.")
                    entity["entity_images"] = []
                    continue

                # 构建 prompt
                item_name = entity.get("item_name", "未知实体")
                visual_desc = "；".join(entity.get("details", {}).get("visual", []))
                semantic_desc = "；".join(entity.get("details", {}).get("semantic", []))
                interaction = entity.get("interaction", "出现在场景中")
                entity_type = "人物" if group == "key_persons" else "物品"

                prompt_parts = [
                    f"活动背景：{activity_desc}。",
                    f"当前场景：{event.get('description', '未命名场景')}。",
                    f"目标{entity_type}：{item_name}。",
                ]
                if visual_desc:
                    prompt_parts.append(f"其外观特征包括：{visual_desc}。")
                if semantic_desc:
                    prompt_parts.append(f"语义角色为：{semantic_desc}。")
                prompt_parts.append(f"该{entity_type}{interaction}。")
                prompt_parts.append(
                    "请将输入图像转换为手绘插画风格，突出显示该目标对象，"
                    "保留其关键轮廓和特征，背景适度简化并虚化，"
                    "整体色调柔和，增强视觉记忆线索，但不得添加任何文字或水印。"
                )
                prompt = "".join(prompt_parts)

                try:
                    enhanced_img = generator.generate_image(
                        prompt=prompt[:800],
                        input_image_path=ref_frame_path,
                        scale=0.65,
                    )
                    entity["entity_images"] = [enhanced_img]  # ← 关键：写回路径！
                    print(f"AIGC enhanced for {item_name}: {enhanced_img}")
                except Exception as e:
                    entity["entity_images"] = []
                    raise NotImplementedError(f"Failed to enhance {item_name}: {e}")
                    



def main(video_path: str):
    load_dotenv()  # 加载 .env 文件中的环境变量

    # === 1. 初始化 TwelveLabs 客户端 ===
    twelvelabs_api_key = os.getenv("TWELVELABS_API_KEY")
    if not twelvelabs_api_key:
        raise ValueError("TWELVELABS_API_KEY not found in .env")

    client = TwelveLabs(api_key=twelvelabs_api_key)

    # === 2. 获取或创建 Index ===
    index_id = os.getenv("INDEX_ID")
    if not index_id:
        print("INDEX_ID not set, creating new index...")
        index_id = create_index(client, index_name="lifelog-index-v1")
        # 可选：将新 index_id 写入 .env（不推荐自动写，但可提示用户）
        print(f"💡 Please add this to your .env: INDEX_ID={index_id}")
    else:
        print(f"Using existing index: {index_id}")

    # === 3. 初始化 VideoProcessor ===
    video_processor = VideoProcessor(
        client=client,
        index_id=index_id,
        video_path=video_path,
    )

    # === 4. 构建活动上下文 ===
    ctx = ActivityContext(
        activity="去超市购物",
        people="我和朋友",
        time="2024年6月15日下午5点",
        location="胖东来",
        sub_event=None,
    )

    # === 5. 执行视频分析 ===
    full_context = execute_video_processor(ctx, video_processor)

    volc_access_key = os.getenv("VOLC_ACCESS_KEY")
    volc_secret_key = os.getenv("VOLC_SECRET_KEY")

    # Step 1: 抽帧（为 AIGC 准备输入）
    extract_reference_frames(full_context, video_path)

    # Step 2: AIGC 增强（如果密钥存在）
    if volc_access_key and volc_secret_key:
        generator = VolcEngineAIGCGenerator(
            access_key=volc_access_key,
            secret_key=volc_secret_key,
            output_dir="output/enhanced",
            add_logo=False,
            add_aigc_meta=True,
        )
        execute_aigc_enhancement(full_context, generator)

    # Step 3: 分离并保存三个 JSON

    # 1. activity_context.json
    activity_only = {
        "activity": full_context["activity"],
        "people": full_context["people"],
        "time": full_context["time"],
        "location": full_context["location"],
        "activity_description": full_context["activity_description"],
        "events": {
            eid: {
                "description": e["description"],
                "start_time": e["start_time"],
                "end_time": e["end_time"],
                "scene_clues": e.get("scene_clues", []),
            }
            for eid, e in full_context["events"].items()
        }
    }
    with open("output/activity_context.json", "w", encoding="utf-8") as f:
        json.dump(activity_only, f, ensure_ascii=False, indent=2)

    # 2. entity_context.json（不含 entity_images）
    entity_only = {}
    for eid, event in full_context["events"].items():
        entity_only[eid] = {
            "key_persons": [
                {k: v for k, v in p.items() if k != "entity_images"}
                for p in event.get("key_persons", [])
            ],
            "key_objects": [
                {k: v for k, v in o.items() if k != "entity_images"}
                for o in event.get("key_objects", [])
            ],
        }
    with open("output/entity_context.json", "w", encoding="utf-8") as f:
        json.dump(entity_only, f, ensure_ascii=False, indent=2)

    # 3. aigc_enhanced_context.json（含 entity_images）
    with open("output/aigc_enhanced_context.json", "w", encoding="utf-8") as f:
        json.dump(full_context, f, ensure_ascii=False, indent=2)

    print("✅ Three JSON files saved:")
    print("   - output/activity_context.json")
    print("   - output/entity_context.json")
    print("   - output/aigc_enhanced_context.json")


if __name__ == "__main__":
    video_path = r"D:/VSCode/VSProj/LifelogProj/Videos/short_version2min.mp4"
    os.makedirs("output", exist_ok=True)
    main(video_path)