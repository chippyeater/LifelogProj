from typing import Any, Dict, List

from utils.frame_utils import parse_timestamp_to_seconds


def build_stage1_template() -> Dict[str, Any]:
    # 阶段1输出局部时间结构，不在这一层做最终事件和实体决策
    return {
        "schema_version": "stage1_v1",
        "metadata": {
            "activity_name": "",
            "time_info": "",
            "location_info": "",
            "source_video": "",
            "source": "manual",
        },
        "timeline": [
            {
                "start_time": "00:01:00.000",
                "end_time": "00:01:12.000",
                "behavior_goal": "挑选饮品",
                "segment_description": "在冷藏区查看并挑选饮品。",
                "reason": "同一行为目标",
                "keyframes": [
                    {
                        "frame_id": "f_001",
                        "frame_time": "00:01:05.000",
                        "interaction_candidates": [
                            {
                                "object_name": "牛奶",
                                "action_name": "拿起查看",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def build_stage2_template() -> Dict[str, Any]:
    # 阶段2同时完成事件重建和最终实体筛选
    return {
        "schema_version": "stage2_v1",
        "metadata": {
            "activity_name": "",
            "time_info": "",
            "location_info": "",
            "source_video": "",
            "source": "stage1",
        },
        "events": [
            {
                "event_id": "e_001",
                "event_name": "挑选饮品",
                "event_description": "在冷藏区查看并挑选饮品。",
                "start_time": "00:01:00.000",
                "end_time": "00:01:12.000",
                "reason": "同一行为目标",
                "source_segment_indices": [0],
                "representative_frame_ids": ["f_001"],
                "selected_entities": [
                    {
                        "entity_id": "e_001_ent_001",
                        "entity_name": "牛奶",
                        "entity_type": "OBJECT",
                        "selection_reason": "与该事件主行为直接相关",
                        "evidence_frame_ids": ["f_001"],
                        "anchor_frame_id": "f_001",
                    }
                ],
            }
        ],
    }


def build_stage3_template() -> Dict[str, Any]:
    # 阶段3按实体生成单点细节题
    return {
        "schema_version": "stage3_v1",
        "metadata": {
            "activity_name": "",
            "time_info": "",
            "location_info": "",
            "source_video": "",
            "source": "stage2",
        },
        "entity_details": [
            {
                "entity_id": "e_001_ent_001",
                "event_id": "e_001",
                "event_name": "挑选饮品",
                "entity_name": "牛奶",
                "anchor_frame_id": "f_001",
                "detail_items": [
                    {
                        "detail_id": "e_001_ent_001_d_001",
                        "question": "这盒牛奶的包装主色是什么？",
                        "correct_answer": "白色",
                        "distractors": ["红色", "绿色", "蓝色"],
                    }
                ],
            }
        ],
    }


def validate_stage1_schema(data: Dict[str, Any]) -> List[str]:
    # 先做结构校验，再补最基础的时间一致性校验
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["阶段1结果必须是字典"]

    if data.get("schema_version") != "stage1_v1":
        errors.append("schema_version 必须是 stage1_v1")

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata 必须存在且为字典")
    else:
        for key in ("activity_name", "time_info", "location_info", "source_video"):
            if key not in metadata:
                errors.append(f"metadata 缺少 {key}")

    timeline = data.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        errors.append("timeline 必须存在且至少包含一个时间段")
        return errors

    previous_end_seconds = None
    for segment_index, segment in enumerate(timeline, start=1):
        if not isinstance(segment, dict):
            errors.append(f"timeline[{segment_index}] 必须是字典")
            continue

        start_time = segment.get("start_time")
        end_time = segment.get("end_time")
        if not start_time:
            errors.append(f"timeline[{segment_index}] 缺少 start_time")
        if not end_time:
            errors.append(f"timeline[{segment_index}] 缺少 end_time")
        if not segment.get("behavior_goal"):
            errors.append(f"timeline[{segment_index}] 缺少 behavior_goal")
        if not segment.get("segment_description"):
            errors.append(f"timeline[{segment_index}] 缺少 segment_description")
        if not segment.get("reason"):
            errors.append(f"timeline[{segment_index}] 缺少 reason")

        start_seconds = None
        end_seconds = None
        if start_time and end_time:
            try:
                start_seconds = parse_timestamp_to_seconds(start_time)
                end_seconds = parse_timestamp_to_seconds(end_time)
                if start_seconds >= end_seconds:
                    errors.append(f"timeline[{segment_index}] start_time 必须早于 end_time")
                if previous_end_seconds is not None and start_seconds < previous_end_seconds:
                    errors.append(f"timeline[{segment_index}] 与上一个时间段重叠或顺序错误")
            except ValueError:
                errors.append(f"timeline[{segment_index}] 时间格式不合法")

        keyframes = segment.get("keyframes")
        if not isinstance(keyframes, list) or not keyframes:
            errors.append(f"timeline[{segment_index}] 必须包含至少一个 keyframe")
            if end_seconds is not None:
                previous_end_seconds = end_seconds
            continue

        for frame_index, keyframe in enumerate(keyframes, start=1):
            if not isinstance(keyframe, dict):
                errors.append(f"timeline[{segment_index}].keyframes[{frame_index}] 必须是字典")
                continue
            if not keyframe.get("frame_id"):
                errors.append(f"timeline[{segment_index}].keyframes[{frame_index}] 缺少 frame_id")
            if not keyframe.get("frame_time"):
                errors.append(f"timeline[{segment_index}].keyframes[{frame_index}] 缺少 frame_time")
            elif start_seconds is not None and end_seconds is not None:
                try:
                    frame_seconds = parse_timestamp_to_seconds(keyframe["frame_time"])
                    if frame_seconds < start_seconds or frame_seconds > end_seconds:
                        errors.append(
                            f"timeline[{segment_index}].keyframes[{frame_index}] frame_time 不在所属时间段内"
                        )
                except ValueError:
                    errors.append(
                        f"timeline[{segment_index}].keyframes[{frame_index}] frame_time 格式不合法"
                    )

            candidates = keyframe.get("interaction_candidates")
            if not isinstance(candidates, list):
                errors.append(
                    f"timeline[{segment_index}].keyframes[{frame_index}] interaction_candidates 必须是列表"
                )
                continue
            for candidate_index, candidate in enumerate(candidates, start=1):
                if not isinstance(candidate, dict):
                    errors.append(
                        f"timeline[{segment_index}].keyframes[{frame_index}].interaction_candidates[{candidate_index}] 必须是字典"
                    )
                    continue
                if not candidate.get("object_name"):
                    errors.append(
                        f"timeline[{segment_index}].keyframes[{frame_index}].interaction_candidates[{candidate_index}] 缺少 object_name"
                    )
                if not candidate.get("action_name"):
                    errors.append(
                        f"timeline[{segment_index}].keyframes[{frame_index}].interaction_candidates[{candidate_index}] 缺少 action_name"
                    )

        if end_seconds is not None:
            previous_end_seconds = end_seconds

    return errors


def validate_stage2_schema(data: Dict[str, Any]) -> List[str]:
    # 阶段2同时校验事件结构和最终实体绑定
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["阶段2结果必须是字典"]

    if data.get("schema_version") != "stage2_v1":
        errors.append("schema_version 必须是 stage2_v1")

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata 必须存在且为字典")

    events = data.get("events")
    if not isinstance(events, list) or not events:
        errors.append("events 必须存在且至少包含一个事件")
        return errors

    previous_end_seconds = None
    for event_index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            errors.append(f"events[{event_index}] 必须是字典")
            continue

        for key in (
            "event_id",
            "event_name",
            "event_description",
            "start_time",
            "end_time",
            "reason",
        ):
            if not event.get(key):
                errors.append(f"events[{event_index}] 缺少 {key}")

        start_time = event.get("start_time")
        end_time = event.get("end_time")
        if start_time and end_time:
            try:
                start_seconds = parse_timestamp_to_seconds(start_time)
                end_seconds = parse_timestamp_to_seconds(end_time)
                if start_seconds >= end_seconds:
                    errors.append(f"events[{event_index}] start_time 必须早于 end_time")
                if previous_end_seconds is not None and start_seconds < previous_end_seconds:
                    errors.append(f"events[{event_index}] 与上一个事件重叠或顺序错误")
                previous_end_seconds = end_seconds
            except ValueError:
                errors.append(f"events[{event_index}] 时间格式不合法")

        if not isinstance(event.get("source_segment_indices"), list) or not event.get("source_segment_indices"):
            errors.append(f"events[{event_index}] source_segment_indices 必须是非空列表")
        if not isinstance(event.get("representative_frame_ids"), list) or not event.get("representative_frame_ids"):
            errors.append(f"events[{event_index}] representative_frame_ids 必须是非空列表")

        entities = event.get("selected_entities")
        if not isinstance(entities, list) or not entities:
            errors.append(f"events[{event_index}] selected_entities 必须是非空列表")
            continue

        for entity_index, entity in enumerate(entities, start=1):
            if not isinstance(entity, dict):
                errors.append(f"events[{event_index}].selected_entities[{entity_index}] 必须是字典")
                continue
            for key in (
                "entity_id",
                "entity_name",
                "entity_type",
                "selection_reason",
                "anchor_frame_id",
            ):
                if not entity.get(key):
                    errors.append(f"events[{event_index}].selected_entities[{entity_index}] 缺少 {key}")
            evidence_frame_ids = entity.get("evidence_frame_ids")
            if not isinstance(evidence_frame_ids, list) or not evidence_frame_ids:
                errors.append(
                    f"events[{event_index}].selected_entities[{entity_index}] evidence_frame_ids 必须是非空列表"
                )
            elif entity.get("anchor_frame_id") and entity["anchor_frame_id"] not in evidence_frame_ids:
                errors.append(
                    f"events[{event_index}].selected_entities[{entity_index}] anchor_frame_id 必须包含在 evidence_frame_ids 中"
                )

    return errors


def validate_stage3_schema(data: Dict[str, Any]) -> List[str]:
    # 阶段3只校验逐实体 detail 结果
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["阶段3结果必须是字典"]

    if data.get("schema_version") != "stage3_v1":
        errors.append("schema_version 必须是 stage3_v1")

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata 必须存在且为字典")

    entity_details = data.get("entity_details")
    if not isinstance(entity_details, list) or not entity_details:
        errors.append("entity_details 必须存在且至少包含一个实体细节结果")
        return errors

    for bundle_index, bundle in enumerate(entity_details, start=1):
        if not isinstance(bundle, dict):
            errors.append(f"entity_details[{bundle_index}] 必须是字典")
            continue
        for key in ("entity_id", "event_id", "event_name", "entity_name", "anchor_frame_id"):
            if not bundle.get(key):
                errors.append(f"entity_details[{bundle_index}] 缺少 {key}")
        detail_items = bundle.get("detail_items")
        if not isinstance(detail_items, list) or not detail_items:
            errors.append(f"entity_details[{bundle_index}] detail_items 必须是非空列表")
            continue
        for item_index, item in enumerate(detail_items, start=1):
            if not isinstance(item, dict):
                errors.append(f"entity_details[{bundle_index}].detail_items[{item_index}] 必须是字典")
                continue
            if not item.get("detail_id"):
                errors.append(f"entity_details[{bundle_index}].detail_items[{item_index}] 缺少 detail_id")
            if not item.get("question"):
                errors.append(f"entity_details[{bundle_index}].detail_items[{item_index}] 缺少 question")
            if not item.get("correct_answer"):
                errors.append(f"entity_details[{bundle_index}].detail_items[{item_index}] 缺少 correct_answer")
            distractors = item.get("distractors")
            if not isinstance(distractors, list) or not distractors:
                errors.append(f"entity_details[{bundle_index}].detail_items[{item_index}] distractors 必须是非空列表")

    return errors
