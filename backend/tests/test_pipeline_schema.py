import unittest

from pipeline_schema import (
    build_stage1_template,
    build_stage2_template,
    build_stage3_template,
    validate_stage1_schema,
    validate_stage2_schema,
    validate_stage3_schema,
)


class PipelineSchemaTest(unittest.TestCase):
    # 测试新的 3-stage 结构模板和最小校验

    def test_build_stage1_template_contains_core_fields(self):
        template = build_stage1_template()
        self.assertEqual(template["schema_version"], "stage1_v1")
        self.assertIn("metadata", template)
        self.assertIn("timeline", template)
        self.assertTrue(template["timeline"])

    def test_build_stage2_template_contains_core_fields(self):
        template = build_stage2_template()
        self.assertEqual(template["schema_version"], "stage2_v1")
        self.assertIn("metadata", template)
        self.assertIn("events", template)
        self.assertIn("selected_entities", template["events"][0])

    def test_build_stage3_template_contains_core_fields(self):
        template = build_stage3_template()
        self.assertEqual(template["schema_version"], "stage3_v1")
        self.assertIn("metadata", template)
        self.assertIn("entity_details", template)
        self.assertTrue(template["entity_details"])

    def test_validate_stage1_schema_accepts_minimal_valid_data(self):
        data = build_stage1_template()
        self.assertEqual(validate_stage1_schema(data), [])

    def test_validate_stage1_schema_rejects_missing_timeline(self):
        data = build_stage1_template()
        data["timeline"] = []
        errors = validate_stage1_schema(data)
        self.assertIn("timeline 必须存在且至少包含一个时间段", errors)

    def test_validate_stage1_schema_rejects_invalid_time_order(self):
        data = build_stage1_template()
        data["timeline"][0]["start_time"] = "00:00:05.000"
        data["timeline"][0]["end_time"] = "00:00:01.000"
        errors = validate_stage1_schema(data)
        self.assertIn("timeline[1] start_time 必须早于 end_time", errors)

    def test_validate_stage1_schema_rejects_frame_outside_segment(self):
        data = build_stage1_template()
        data["timeline"][0]["start_time"] = "00:00:01.000"
        data["timeline"][0]["end_time"] = "00:00:05.000"
        data["timeline"][0]["keyframes"][0]["frame_time"] = "00:00:06.000"
        errors = validate_stage1_schema(data)
        self.assertIn("timeline[1].keyframes[1] frame_time 不在所属时间段内", errors)

    def test_validate_stage1_schema_rejects_overlapping_segments(self):
        data = build_stage1_template()
        data["timeline"].append(
            {
                "start_time": "00:01:10.000",
                "end_time": "00:01:20.000",
                "behavior_goal": "第二段",
                "segment_description": "第二段。",
                "reason": "测试",
                "keyframes": [
                    {
                        "frame_id": "f_002",
                        "frame_time": "00:01:15.000",
                        "interaction_candidates": [{"object_name": "黄瓜", "action_name": "拿起查看"}],
                    }
                ],
            }
        )
        errors = validate_stage1_schema(data)
        self.assertIn("timeline[2] 与上一个时间段重叠或顺序错误", errors)

    def test_validate_stage2_schema_accepts_minimal_valid_data(self):
        data = build_stage2_template()
        self.assertEqual(validate_stage2_schema(data), [])

    def test_validate_stage2_schema_rejects_overlapping_events(self):
        data = build_stage2_template()
        data["events"].append(
            {
                "event_id": "e_002",
                "event_name": "第二事件",
                "event_description": "第二事件描述。",
                "start_time": "00:01:10.000",
                "end_time": "00:01:20.000",
                "reason": "测试",
                "source_segment_indices": [1],
                "representative_frame_ids": ["f_002"],
                "selected_entities": [
                    {
                        "entity_id": "e_002_ent_001",
                        "entity_name": "酸奶",
                        "entity_type": "OBJECT",
                        "selection_reason": "与当前事件主行为直接相关",
                        "evidence_frame_ids": ["f_002"],
                        "anchor_frame_id": "f_002",
                    }
                ],
            }
        )
        errors = validate_stage2_schema(data)
        self.assertIn("events[2] 与上一个事件重叠或顺序错误", errors)

    def test_validate_stage2_schema_rejects_missing_anchor(self):
        data = build_stage2_template()
        data["events"][0]["selected_entities"][0]["anchor_frame_id"] = "f_999"
        errors = validate_stage2_schema(data)
        self.assertIn(
            "events[1].selected_entities[1] anchor_frame_id 必须包含在 evidence_frame_ids 中",
            errors,
        )

    def test_validate_stage3_schema_accepts_minimal_valid_data(self):
        data = build_stage3_template()
        self.assertEqual(validate_stage3_schema(data), [])

    def test_validate_stage3_schema_rejects_missing_distractors(self):
        data = build_stage3_template()
        data["entity_details"][0]["detail_items"][0]["distractors"] = []
        errors = validate_stage3_schema(data)
        self.assertIn("entity_details[1].detail_items[1] distractors 必须是非空列表", errors)
