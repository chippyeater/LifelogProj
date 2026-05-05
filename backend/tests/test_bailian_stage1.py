import json
import os
import unittest

from bailian_video_processor import BailianVideoProcessor, _normalize_stage1_video_parse
from my_basics import ActivityContext
from tests.test_support import clear_temp_root, prepare_temp_dir


class BailianStage1Test(unittest.TestCase):
    # 测试阶段1专用输出的归一化和落盘逻辑

    def setUp(self):
        clear_temp_root()
        self.output_dir = prepare_temp_dir("bailian_stage1")
        self.ctx = ActivityContext(
            activity="逛超市",
            people="我",
            time="2026年04月11日15:00",
            location="盒马鲜生",
            video_length="00:02:00",
        )

    def test_normalize_stage1_video_parse(self):
        raw = {
            "timeline": [
                {
                    "start_time": "00:00:01.000",
                    "end_time": "00:00:05.000",
                    "behavior_goal": "进入超市并拿购物篮",
                    "segment_description": "在入口处拿购物篮并进入超市。",
                    "reason": "这一段的行为目标稳定，都是进入超市前的准备动作。",
                    "keyframes": [
                        {
                            "frame_time": "00:00:03.000",
                            "interaction_candidates": [
                                {
                                    "object_name": "购物篮",
                                    "action_name": "拿起",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        normalized = _normalize_stage1_video_parse(raw, self.ctx)
        self.assertEqual(normalized["schema_version"], "stage1_v1")
        self.assertEqual(normalized["metadata"]["activity_name"], "逛超市")
        self.assertEqual(normalized["timeline"][0]["keyframes"][0]["frame_id"], "f_001")
        self.assertEqual(normalized["timeline"][0]["behavior_goal"], "进入超市并拿购物篮")
        self.assertEqual(
            normalized["timeline"][0]["keyframes"][0]["interaction_candidates"][0]["object_name"],
            "购物篮",
        )

    def test_analyze_stage1_video_parse_writes_raw_response(self):
        processor = object.__new__(BailianVideoProcessor)
        processor.output_dir = self.output_dir
        processor._chat = lambda prompt: json.dumps(
            {
                "timeline": [
                    {
                        "start_time": "00:00:01.000",
                        "end_time": "00:00:05.000",
                        "behavior_goal": "进入超市并拿购物篮",
                        "segment_description": "在入口处拿购物篮并进入超市。",
                        "reason": "这一段的行为目标稳定，都是进入超市前的准备动作。",
                        "keyframes": [
                            {
                                "frame_time": "00:00:03.000",
                                "interaction_candidates": [
                                    {
                                        "object_name": "购物篮",
                                        "action_name": "拿起",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )

        result = processor.analyze_stage1_video_parse(self.ctx)
        raw_path = os.path.join(self.output_dir, "bailian", "raw_stage1_video_parse_resp.txt")

        self.assertTrue(os.path.exists(raw_path))
        self.assertEqual(result["schema_version"], "stage1_v1")
        self.assertEqual(result["timeline"][0]["keyframes"][0]["frame_id"], "f_001")
