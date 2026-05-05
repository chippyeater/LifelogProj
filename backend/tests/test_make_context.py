import json
import unittest
from pathlib import Path

from my_basics import ActivityContext
from tests.test_support import clear_temp_root, import_with_stubs, prepare_temp_dir


make_context = import_with_stubs(
    "make_context",
    {
        "clue_aigc_generator": {
            "VolcEngineAIGCGenerator": object,
        },
        "bailian_video_processor": {
            "BailianVideoProcessor": object,
        },
    },
)


class _FakeProcessor:
    # 用最小假处理器验证单次抽取和 checkpoint 写入
    def __init__(self, payload):
        self.payload = payload

    def analyze_full_context(self, context):
        return self.payload


class MakeContextTest(unittest.TestCase):
    def tearDown(self):
        clear_temp_root()

    def test_execute_video_processor_single_call_writes_checkpoint(self):
        sample_path = Path("output") / "001" / "extracted_context.json"
        payload = json.loads(sample_path.read_text(encoding="utf-8"))
        processor = _FakeProcessor(payload)
        context = ActivityContext(
            activity="逛超市",
            people="我",
            time="2026年04月11日12:00",
            location="超市",
            video_length="00:10:00",
        )

        temp_dir = prepare_temp_dir("make_context_checkpoint")
        checkpoint_path = str(temp_dir / "extracted_context.json")
        result = make_context.execute_video_processor_single_call(
            context,
            processor,
            checkpoint_path,
        )

        self.assertEqual(result["activity"], payload["activity"])
        self.assertTrue(Path(checkpoint_path).exists())

        saved = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["activity"], payload["activity"])
        self.assertEqual(len(saved.get("events") or {}), 7)

    def test_extract_reference_frames_with_case_001(self):
        source_context_path = Path("output") / "001" / "extracted_context.json"
        video_path = Path("videos") / "001.mp4"
        full_context = json.loads(source_context_path.read_text(encoding="utf-8"))

        # 只保留一个最小事件，避免测试跑太久
        first_event_key = next(iter(full_context["events"]))
        first_event = full_context["events"][first_event_key]
        first_scene = (first_event.get("scene_clues") or [])[0]
        first_entity = (first_event.get("entities") or [])[0]
        trimmed_context = {
            "activity": full_context.get("activity"),
            "activity_visual_clue": [],
            "events": {
                first_event_key: {
                    "name": first_event.get("name"),
                    "description": first_event.get("description"),
                    "scene_clues": [first_scene],
                    "entities": [first_entity],
                }
            },
        }

        temp_dir = prepare_temp_dir("make_context_frames")
        frames_dir = temp_dir / "frames"
        make_context.extract_reference_frames(
            trimmed_context,
            str(video_path),
            str(frames_dir),
        )

        updated_event = trimmed_context["events"][first_event_key]
        scene_path = updated_event["scene_clues"][0].get("reference_frame_path")
        entity_path = updated_event["entities"][0].get("reference_frame_path")

        self.assertTrue(scene_path)
        self.assertTrue(entity_path)
        self.assertTrue((temp_dir / scene_path).exists())
        self.assertTrue((temp_dir / entity_path).exists())

    def test_extract_reference_frames_reuses_existing_images(self):
        temp_dir = prepare_temp_dir("make_context_reuse_frames")
        frames_dir = temp_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        existing_frame = frames_dir / "购物篮.jpg"
        existing_frame.write_bytes(b"existing-frame")

        trimmed_context = {
            "activity": "逛超市",
            "activity_visual_clue": [],
            "events": {
                "event_001": {
                    "name": "进入超市",
                    "description": "进入超市并拿购物篮",
                    "scene_clues": [],
                    "entities": [
                        {
                            "item_name": "购物篮",
                            "frame": "00:00:06",
                            "reference_frame_path": "frames/购物篮.jpg",
                        }
                    ],
                }
            },
        }

        make_context.extract_reference_frames(
            trimmed_context,
            str(Path("videos") / "001.mp4"),
            str(frames_dir),
        )

        entity = trimmed_context["events"]["event_001"]["entities"][0]
        self.assertEqual(entity.get("reference_frame_path"), "frames/购物篮.jpg")
        self.assertEqual(existing_frame.read_bytes(), b"existing-frame")
