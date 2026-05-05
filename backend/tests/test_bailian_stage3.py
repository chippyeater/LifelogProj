import base64
import json
import os
import unittest

from bailian_video_processor import BailianVideoProcessor, _normalize_stage3_detail_generate
from pipeline_schema import build_stage1_template, build_stage2_template
from tests.test_support import clear_temp_root, prepare_temp_dir


class BailianStage3Test(unittest.TestCase):
    # 测试新的阶段3：按实体生成 detail

    def setUp(self):
        clear_temp_root()
        self.output_dir = prepare_temp_dir("bailian_stage3")

        self.stage1_output = build_stage1_template()
        self.stage1_output["metadata"]["activity_name"] = "逛超市"
        self.stage1_output["metadata"]["time_info"] = "2026年4月12日15:00"
        self.stage1_output["metadata"]["location_info"] = "盒马鲜生"
        self.stage1_output["metadata"]["source_video"] = "videos/005.mp4"
        self.stage1_output["timeline"][0]["keyframes"][0]["frame_id"] = "f_001"
        self.stage1_output["timeline"][0]["keyframes"][0]["frame_time"] = "00:01:05.000"
        self.stage1_output["timeline"][0]["keyframes"][0]["interaction_candidates"] = [
            {"object_name": "牛奶", "action_name": "拿起查看"}
        ]

        frame_dir = os.path.join(self.output_dir, "pipeline_cache", "stage1_video_parse", "frames")
        os.makedirs(frame_dir, exist_ok=True)
        frame_path = os.path.join(frame_dir, "f_001.jpg")
        with open(frame_path, "wb") as f:
            f.write(
                base64.b64decode(
                    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQEBAQEA8QDw8PDw8PDw8PDw8PDw8PFREWFhURFRUYHSggGBolGxUVITEhJSkrLi4uFx8zODMsNygtLisBCgoKDg0OGhAQGi0mICYtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLf/AABEIAAEAAgMBIgACEQEDEQH/xAAXAAEBAQEAAAAAAAAAAAAAAAAAAQID/8QAFhEBAQEAAAAAAAAAAAAAAAAAAQAC/9oADAMBAAIQAxAAAAGkE3//xAAYEAACAwAAAAAAAAAAAAAAAAAAAREhMf/aAAgBAQABBQJqf//EABYRAQEBAAAAAAAAAAAAAAAAAAABEf/aAAgBAwEBPwGn/8QAFhEBAQEAAAAAAAAAAAAAAAAAABEB/9oACAECAQE/AYf/xAAYEAEAAwEAAAAAAAAAAAAAAAABABEhMf/aAAgBAQAGPwKc1//EABoQAQACAwEAAAAAAAAAAAAAAAERIQAxQWH/2gAIAQEAAT8hJQ8xvJ1rP//aAAwDAQACAAMAAAAQ8//EABcRAAMBAAAAAAAAAAAAAAAAAAABESH/2gAIAQMBAT8Qqf/EABcRAQEBAQAAAAAAAAAAAAAAAAERADH/2gAIAQIBAT8QXUf/xAAbEAEBAQADAQEAAAAAAAAAAAABEQAhMUFhcf/aAAgBAQABPxBfPuhdQm7QF8iUu2mq2x8m3//Z"
                )
            )
        self.stage1_output["timeline"][0]["keyframes"][0]["image_path"] = (
            "pipeline_cache/stage1_video_parse/frames/f_001.jpg"
        )

        self.stage2_output = build_stage2_template()
        self.stage2_output["metadata"]["activity_name"] = "逛超市"
        self.stage2_output["metadata"]["time_info"] = "2026年4月12日15:00"
        self.stage2_output["metadata"]["location_info"] = "盒马鲜生"
        self.stage2_output["metadata"]["source_video"] = "videos/005.mp4"
        self.stage2_output["events"][0]["selected_entities"][0]["entity_id"] = "e_001_ent_001"
        self.stage2_output["events"][0]["selected_entities"][0]["entity_name"] = "牛奶"
        self.stage2_output["events"][0]["selected_entities"][0]["entity_type"] = "OBJECT"
        self.stage2_output["events"][0]["selected_entities"][0]["selection_reason"] = "与当前事件主行为直接相关"
        self.stage2_output["events"][0]["selected_entities"][0]["evidence_frame_ids"] = ["f_001"]
        self.stage2_output["events"][0]["selected_entities"][0]["anchor_frame_id"] = "f_001"

    def test_normalize_stage3_detail_generate(self):
        raw = {
            "entity_details": [
                {
                    "entity_id": "e_001_ent_001",
                    "event_id": "e_001",
                    "entity_name": "牛奶",
                    "anchor_frame_id": "f_001",
                    "detail_items": [
                        {
                            "question": "这盒牛奶的包装主色是什么？",
                            "correct_answer": "白色",
                            "distractors": ["红色", "绿色", "蓝色"],
                        }
                    ],
                }
            ]
        }
        normalized = _normalize_stage3_detail_generate(raw, self.stage2_output)
        self.assertEqual(normalized["schema_version"], "stage3_v1")
        self.assertEqual(normalized["entity_details"][0]["detail_items"][0]["detail_id"], "e_001_ent_001_d_001")

    def test_analyze_stage3_detail_generate_writes_raw_response(self):
        processor = object.__new__(BailianVideoProcessor)
        processor.output_dir = self.output_dir
        processor._chat_stage3_with_images = lambda prompt, image_paths: json.dumps(
            {
                "detail_items": [
                    {
                        "question": "这盒牛奶的包装主色是什么？",
                        "correct_answer": "白色",
                        "distractors": ["红色", "绿色", "蓝色"],
                    }
                ]
            },
            ensure_ascii=False,
        )

        result = processor.analyze_stage3_detail_generate(self.stage1_output, self.stage2_output)
        raw_path = os.path.join(self.output_dir, "bailian", "raw_stage3_detail_generate_e_001_ent_001.txt")

        self.assertTrue(os.path.exists(raw_path))
        self.assertEqual(result["schema_version"], "stage3_v1")
        self.assertEqual(result["entity_details"][0]["detail_items"][0]["correct_answer"], "白色")
