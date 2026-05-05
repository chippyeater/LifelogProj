import base64
import json
import os
import unittest
from unittest.mock import patch

import bailian_video_processor
from bailian_video_processor import (
    BailianVideoProcessor,
    _normalize_stage2_event_rebuild,
    _resolve_stage1_image_paths,
)
from pipeline_schema import build_stage1_template
from tests.test_support import clear_temp_root, prepare_temp_dir


class BailianStage2Test(unittest.TestCase):
    # 测试新的阶段2：事件重建和实体筛选合并输出

    def setUp(self):
        clear_temp_root()
        self.output_dir = prepare_temp_dir("bailian_stage2")
        self.stage1_output = build_stage1_template()
        self.stage1_output["metadata"]["activity_name"] = "逛超市"
        self.stage1_output["metadata"]["time_info"] = "2026年4月12日15:00"
        self.stage1_output["metadata"]["location_info"] = "盒马鲜生"
        self.stage1_output["metadata"]["source_video"] = "videos/005.mp4"
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

    def test_normalize_stage2_event_rebuild(self):
        raw = {
            "events": [
                {
                    "event_name": "挑选饮品",
                    "event_description": "在冷藏区查看并挑选饮品。",
                    "start_time": "00:01:00.000",
                    "end_time": "00:01:12.000",
                    "reason": "同一行为目标",
                    "source_segment_indices": [0],
                    "representative_frame_ids": ["f_001"],
                    "selected_entities": [
                        {
                            "entity_name": "牛奶",
                            "entity_type": "OBJECT",
                            "selection_reason": "与当前事件主行为直接相关",
                            "evidence_frame_ids": ["f_001"],
                            "anchor_frame_id": "f_001",
                        }
                    ],
                }
            ]
        }
        normalized = _normalize_stage2_event_rebuild(raw, self.stage1_output)
        self.assertEqual(normalized["schema_version"], "stage2_v1")
        self.assertEqual(normalized["events"][0]["event_id"], "e_001")
        self.assertEqual(normalized["events"][0]["selected_entities"][0]["entity_id"], "e_001_ent_001")

    def test_analyze_stage2_event_rebuild_writes_raw_response(self):
        processor = object.__new__(BailianVideoProcessor)
        processor.output_dir = self.output_dir
        processor._chat_stage2_with_images = lambda prompt, image_paths: json.dumps(
            {
                "events": [
                    {
                        "event_name": "挑选饮品",
                        "event_description": "在冷藏区查看并挑选饮品。",
                        "start_time": "00:01:00.000",
                        "end_time": "00:01:12.000",
                        "reason": "同一行为目标",
                        "source_segment_indices": [0],
                        "representative_frame_ids": ["f_001"],
                        "selected_entities": [
                            {
                                "entity_name": "牛奶",
                                "entity_type": "OBJECT",
                                "selection_reason": "与当前事件主行为直接相关",
                                "evidence_frame_ids": ["f_001"],
                                "anchor_frame_id": "f_001",
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )

        result = processor.analyze_stage2_event_rebuild(self.stage1_output)
        raw_path = os.path.join(self.output_dir, "bailian", "raw_stage2_event_rebuild_resp.txt")

        self.assertTrue(os.path.exists(raw_path))
        self.assertEqual(result["schema_version"], "stage2_v1")
        self.assertEqual(result["events"][0]["selected_entities"][0]["entity_name"], "牛奶")

    def test_resolve_stage1_image_paths(self):
        image_paths = _resolve_stage1_image_paths(self.stage1_output, self.output_dir)
        self.assertEqual(len(image_paths), 1)
        self.assertTrue(image_paths[0].endswith("f_001.jpg"))

    @patch.dict(
        os.environ,
        {
            "BAILIAN_API_KEY": "test-key",
            "BAILIAN_MODEL": "stage1-model",
            "BAILIAN_MAX_TOKENS": "1234",
            "BAILIAN_TEMPERATURE": "0.3",
        },
        clear=True,
    )
    def test_stage2_model_config_falls_back_to_default_bailian_config(self):
        with patch.object(bailian_video_processor, "OpenAI") as mock_openai:
            processor = BailianVideoProcessor(video_url="https://example.com/video.mp4", output_dir=self.output_dir)

        self.assertEqual(processor.model, "stage1-model")
        self.assertEqual(processor.stage2_model, "stage1-model")
        self.assertEqual(processor.stage2_max_tokens, 1234)
        self.assertEqual(processor.stage2_temperature, 0.3)
        mock_openai.assert_called_once()
