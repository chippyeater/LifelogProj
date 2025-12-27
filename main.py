from dataclasses import dataclass
import os
import time
import json
from typing import Any, Dict, Optional
import requests
from pathlib import Path
import asyncio
from twelvelabs import AsyncTwelveLabs, ResponseFormat
from twelvelabs import IndexesCreateResponse, TwelveLabs, core
from twelvelabs.indexes import IndexesCreateRequestModelsItem

API_KEY = "tlk_36GR70D23DGHGW23WQ2TK3PFRW5F"
INDEX_NAME = "memory_assistant_index"
INDEX_ID = "6902e65d8ef1cd6b38b81eb5"
VIDEO_ID = "694f9289db0246c06ce26865"
VIDEO_GLOB = "videos/*.mp4"


def create_index(
        client, 
        index_name, 
        model_name="Pegasus1.2", 
        model_options=["visual", "audio"]
        )-> IndexesCreateResponse:
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
    except Exception as e:
        print(f"Error creating index: {e}")
        raise

    print(f"Created index: id={index.id} name={index.name}")
    return index


@dataclass
class ActivityContext:
    activity: str
    people: str
    time: str
    location: str


def build_event_split_prompt(
    ctx: ActivityContext,
    segmentation_rules: str,
    cue_rules: str,
    example_json: Optional[Dict[str, Any]] = None,
) -> str:
    """
    组装 LangGPT 风格 Prompt：Task / Context / Rules / Workflow / Output Format / Example
    """
    task = (
        "对[输入]的第一人称视频进行子事件切分，并对划分出的每个子事件进行描述概括和关键帧线索提取。\n"
        "子事件切分任务定义:基于整体活动主题，将视频中的活动划分为能够构成完整活动流程的关键事件序列。将人脑中把连续的外界信息切分成若于个有意义日互相关联的事件的过程称作事件切分（eventseqmentation），连续信息被切分成不同事件的时间点就是事件边界（event boundary），它标志着前一事件的结束和后一事件的开始。\n"
    )

    context = (
        f"视频中的活动主题是{ctx.activity}，人物是{ctx.people}，时间是{ctx.time}，地点是{ctx.location}。\n"
    )

    rules = (
        "【事件边界划分标准】\n"
        f"{segmentation_rules}\n\n"
        "【事件描述标准】\n"
        "    - 描述内容需包括视频拍摄者在子事件中的所处场景和关键行为，且该子事件需与活动主题高度相关、并具备一定目标和认知负荷\n\n"
        "【划分要求】\n"
        "    1. 划分的事件必须能够做为构成视频拍摄者完整活动流程的关键行为步骤。\n"
        "    2. 若基于行为主题切换来分割事件，则划分出的行为主题需具备行为目标和认知负荷，拍摄者注意力需集中在行为本身，即不能包括用户无意识的行为。正面示例包括“拿外卖”、“点餐”、“进入商场”等；反面示例包括“摆弄头发”。\n"
        "    3. 若基于行为主题切换来分割事件，则识别行为边界时，需保障切割后的两项行为的主题或目标不同。\n"
        "    4. 若基于活动场景切换来分割事件，则识别因镜头抖动、用户身体部位遮挡、以及手机遮挡而导致场景的意外变化，并剔除这一影响。\n"
        "    5. 若基于活动场景切换来分割事件，在识别场景边界时，需保障切割后的两个场景之间可回忆内容（如场景中的关键物品、用户在场景中的关键行为、或用户出现在该场景的目的等）存在切换。\n"
        "    6. “看手机”不可以被识别为事件。\n\n"
        "【线索提取标准】\n"
        f"{cue_rules}\n"
    )

    workflow = (
        "1. 基于【划分标准】与【划分要求】识别事件边界并划分事件。\n"
        "2. 基于【事件描述标准】，对划分出的事件进行概括描述。\n"
        "3. 合并非关键事件：判断事件划分粒度是否合理，如果事件并非构成活动流程的关键行为，则考虑将事件进行合并。例如“通过地铁站闸机”并不能算作关键事件，可以合并到“离开地铁站”这一更大的事件单位中\n"
        "4. 对划分出的每个事件，基于【线索提取标准】构建线索：选取关键帧图像、提取关键帧附近的音频数据、生成语义提示。\n"
        "5. 检查线索内容是否直接透露检索目标，如果是，则重新构建线索。\n"
    )

    output_format = {
        "activity_description": "string",
        "event_count": "integer",
        "events": [
            {
                "id": "integer",
                "description": "string",
                "start_time": "HH:MM:SS",
                "end_time": "HH:MM:SS",
                "scene_clues": [
                    {"frame": "HH:MM:SS", "description": "string"}
                ],
            }
        ],
    }

    example = ""
    if example_json is not None:
        example = "```json\n" + json.dumps(example_json, ensure_ascii=False, indent=2) + "\n```"

    # 统一拼接
    prompt = f"""
# Task
{task}

# Context
{context}

# Rules
{rules}

# Workflow
{workflow}

# Output Format (JSON Schema)
```json
{json.dumps(output_format, ensure_ascii=False, indent=2)}

"""
        # # Example
        # {example if example else "(No example provided)"}
    return prompt


SEGMENTATION_RULES = """\
- 活动场景切换：视频拍摄者的活动场景发生变化时分割事件，如室内-室外场景的切换、不同房间的切换、活动区域的整体切换等。
    - 基于第一人称视频的补充标准
        1. 识别并过滤身体部位遮挡造成对场景切换的误判
        2. 识别并过滤手机遮挡造成对场景切换的误判
- 行为主题切换：视频拍摄者的行为主题或目标发生变化时分割事件，如静止-行走状态的切换、聊天-沉默状态的切换、停止用餐等。
    - 基于第一人称视频的补充标准
        1. 识别并过滤用户的无意识行为，如拨弄头发等
        2. 识别并过滤看手机事件\
"""

CUE_RULES = """\
- 视觉线索（关键帧）：
    1. 画面清晰，画面中无人物身体部位、手机等与事件无关的物体遮挡。
    2. 画面中不包含该场景中的后续实体对象材料。
- 语义线索（文本）：
    场景特点提示（色彩丰富度、感官联想），例如：
    1. 视觉特征：“在超市里，你有没有看到什么颜色特别显眼的区域？”
    2. 温度特征：“你有没有在某个温度比较低的区域停留？”\
"""

def run_pipeline():
    global INDEX_ID, VIDEO_ID, API_KEY, INDEX_NAME, VIDEO_GLOB
    client = TwelveLabs(api_key=API_KEY)

    # # 如果索引不存在创建新索引
    # if INDEX_ID is None:
    #     index = create_index(client, INDEX_NAME)
    #     INDEX_ID = index.id
    #     print(f"Created index: id={index.id}")

    if INDEX_ID:
        # if VIDEO_ID is None:
        #     video_path = r"D:\VSCode\VSProj\LifelogProj\Videos\short_version2min.mp4"
        #     with open(video_path, "rb") as f:
        #         task = client.tasks.create(
        #             index_id=INDEX_ID, 
        #             video_file=("short_version2min.mp4", f, "video/mp4"),
        #         )
        #         print(f"Created task: id={task.id}, video_id={task.video_id}")
        #         VIDEO_ID = task.video_id

        if VIDEO_ID:
            ctx = ActivityContext(
                activity="去超市购物",
                people="我和朋友",
                time="2024年6月15日下午5点",
                location="胖东来",
            )
            prompt = build_event_split_prompt(
                ctx=ctx,
                segmentation_rules=SEGMENTATION_RULES,
                cue_rules=CUE_RULES,
            )
            print("Generated Prompt:")
            print(prompt)
            response = client.analyze(
                video_id=VIDEO_ID, 
                prompt=prompt,
                temperature=0.2,
                response_format=ResponseFormat(
                    json_schema={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "summary": {"type": "string"},
                            "keywords": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                )
            )
            print("Analysis response:")
            print(response)
            print("Analysis response (formatted):")
            print(json.dumps(response, ensure_ascii=False, indent=2))

    
if __name__ == "__main__":
    run_pipeline()