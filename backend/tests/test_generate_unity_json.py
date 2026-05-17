import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_support import clear_temp_root, import_with_stubs, prepare_temp_dir


generate_unity_json = import_with_stubs(
    "generate_unity_json",
    {
        "cv2": {
            "VideoCapture": object,
        },
        "numpy": {
            "zeros": lambda *args, **kwargs: [],
            "dot": lambda *args, **kwargs: 0.0,
            "vstack": lambda rows: rows,
            "max": max,
        },
        "sentence_transformers": {
            "SentenceTransformer": object,
        },
        "clue_aigc_generator": {
            "VolcEngineAIGCGenerator": object,
        },
        "llm_client": {
            "BailianLLM": object,
            "SiliconFlowLLM": object,
        },
    },
)


class _FakeImageGenerator:
    def __init__(self, access_key=None, secret_key=None, output_dir=None, add_logo=False, add_aigc_meta=True):
        self.output_dir = output_dir

    def generate_image_text(self, prompt, scale=2.5, width=512, height=512):
        output_dir = Path(self.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "temp_option.jpg"
        out_path.write_bytes(b"fake-image")
        return str(out_path)


class GenerateUnityJsonTest(unittest.TestCase):
    def tearDown(self):
        clear_temp_root()

    def _run_case(self, case_id: str, expected_event_count: int, expected_stage_count: int):
        sample_input = Path("output") / case_id / "extracted_context.json"
        self.assertTrue(sample_input.exists(), f"missing sample input: {sample_input}")

        temp_output = prepare_temp_dir(f"unity_json_{case_id}")
        input_copy = temp_output / "extracted_context.json"
        sample_data = json.loads(sample_input.read_text(encoding="utf-8"))
        sample_data["source_video"] = f"videos/{case_id}.mp4"
        sample_data["activity_audio_clue"] = {
            "start_time": "00:00:01.000",
            "end_time": "00:00:03.000",
            "reason": "test audio clue",
        }
        for event in (sample_data.get("events") or {}).values():
            event["video_clip"] = {
                "start_time": event.get("start_time") or "00:00:01.000",
                "end_time": event.get("end_time") or "00:00:03.000",
                "reason": "test video clue",
            }
        input_copy.write_text(json.dumps(sample_data, ensure_ascii=False, indent=2), encoding="utf-8")

        with patch.object(generate_unity_json, "_load_embedding_model", return_value=None), \
             patch.object(generate_unity_json, "VolcEngineAIGCGenerator", _FakeImageGenerator), \
             patch.object(generate_unity_json, "_export_activity_audio_asset", return_value="media/audio/activity_theme_audio.mp3"), \
             patch.object(generate_unity_json, "_export_event_video_asset", side_effect=lambda **kwargs: f"media/video/{kwargs['event_id']}.mp4"), \
             patch.object(generate_unity_json, "_generate_distractor_option_with_llm", return_value=["apple", "bread", "drink"]), \
             patch.object(generate_unity_json, "_generate_distractor_events_with_llm", return_value={"event1": "distractor event"}):
            game_meta, game_flow = generate_unity_json.generate_game_meta_flow(
                input_path=str(input_copy),
                output_dir=str(temp_output),
                base_url="http://host",
                user_id=case_id,
            )

        self.assertIn("game_levels", game_meta)
        self.assertIn("game_flow", game_flow)
        self.assertEqual(len(game_meta["game_levels"]), 1)

        narratives = game_meta["game_levels"][0]["narratives"]
        stages = game_flow["game_flow"]

        self.assertEqual(len(narratives), expected_event_count)
        self.assertEqual(len(stages), expected_stage_count)
        self.assertTrue(any(narrative.get("items") for narrative in narratives))
        self.assertEqual(stages[0]["stage_id"], "stage_001")
        self.assertEqual(stages[0]["task_type"], "selection")
        self.assertIn("task_description", stages[0])
        self.assertIn("time_info", stages[0])
        self.assertIn("hints", stages[0])
        self.assertIn("audio_url", stages[0]["hints"])
        self.assertNotIn("description", stages[0])
        self.assertEqual(
            stages[0]["hints"]["audio_url"],
            "media/audio/activity_theme_audio.mp3",
        )
        self.assertIn("video_clip_url", stages[1])
        self.assertTrue(stages[1]["video_clip_url"].startswith("media/video/"))
        detail_stages = [stage for stage in stages if stage.get("task_type") == "detail_recall"]
        self.assertTrue(any(stage.get("correct_answer") for stage in detail_stages))
        self.assertTrue(any(stage.get("advanced_recall_questions") for stage in detail_stages))
        self.assertIn("task_description", stages[-1])
        self.assertTrue((temp_output / "GameMeta.json").exists())
        self.assertTrue((temp_output / "GameFlow.json").exists())

    def test_case_001(self):
        self._run_case("001", expected_event_count=7, expected_stage_count=22)

    def test_case_003(self):
        self._run_case("003", expected_event_count=8, expected_stage_count=25)

    def test_generate_option_images_reuses_existing_file_when_not_regenerating(self):
        temp_output = prepare_temp_dir("reuse_option_image")
        option_dir = temp_output / "option_images"
        option_dir.mkdir(parents=True, exist_ok=True)
        filename = generate_unity_json._build_option_image_name("stage_001", "apple", 1)
        existing_path = option_dir / filename
        existing_path.write_bytes(b"existing-image")

        generator = _FakeImageGenerator(output_dir=str(option_dir))
        with patch.object(generator, "generate_image_text", side_effect=AssertionError("should not regenerate option image")):
            result = generate_unity_json._generate_option_images(
                options=["apple"],
                stage_id="stage_001",
                output_dir=str(option_dir),
                generator=generator,
                regenerate_assets=False,
            )

        self.assertEqual(result, [{"text": "apple", "image_path": f"option_images/{filename}"}])
        self.assertEqual(existing_path.read_bytes(), b"existing-image")

    def test_generate_option_images_reuses_same_filename_for_same_text_across_stages(self):
        filename_a = generate_unity_json._build_option_image_name("stage_001", "逛超市", 1)
        filename_b = generate_unity_json._build_option_image_name("stage_999", "逛超市", 4)

        self.assertEqual(filename_a, "逛超市.jpg")
        self.assertEqual(filename_b, "逛超市.jpg")

    def test_export_media_assets_reuse_existing_files_when_not_regenerating(self):
        temp_output = prepare_temp_dir("reuse_media_assets")
        audio_path = temp_output / "media" / "audio" / "activity_theme_audio.mp3"
        video_filename = generate_unity_json._build_safe_media_filename("event", "e_001", "mp4")
        video_path = temp_output / "media" / "video" / video_filename
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"existing-audio")
        video_path.write_bytes(b"existing-video")

        with patch.object(generate_unity_json, "export_audio_clip", side_effect=AssertionError("should not re-export audio")), \
             patch.object(generate_unity_json, "export_video_clip", side_effect=AssertionError("should not re-export video")):
            audio_relative = generate_unity_json._export_activity_audio_asset(
                source_video_path="videos/005.mp4",
                activity_audio_clue={
                    "start_time": "00:00:01.000",
                    "end_time": "00:00:03.000",
                },
                output_dir=str(temp_output),
                user_id="005",
                regenerate_assets=False,
            )
            video_relative = generate_unity_json._export_event_video_asset(
                source_video_path="videos/005.mp4",
                event_id="e_001",
                video_clip={
                    "start_time": "00:00:01.000",
                    "end_time": "00:00:05.000",
                },
                output_dir=str(temp_output),
                user_id="005",
                regenerate_assets=False,
            )

        self.assertEqual(audio_relative, "media/audio/activity_theme_audio.mp3")
        self.assertEqual(video_relative, f"media/video/{video_filename}")
        self.assertEqual(audio_path.read_bytes(), b"existing-audio")
        self.assertEqual(video_path.read_bytes(), b"existing-video")

    def test_validate_generated_assets_reports_non_ascii_without_failing_when_files_exist(self):
        temp_output = prepare_temp_dir("validate_assets")
        (temp_output / "frames").mkdir(parents=True, exist_ok=True)
        (temp_output / "option_images").mkdir(parents=True, exist_ok=True)
        (temp_output / "frames" / "scene_001.jpg").write_bytes(b"ok")
        (temp_output / "frames" / "中文线索.jpg").write_bytes(b"ok2")
        (temp_output / "option_images" / "safe_option.jpg").write_bytes(b"ok3")

        game_meta = {
            "game_levels": [
                {
                    "hint_scene_images": {
                        "puzzle_main": "frames/scene_001.jpg",
                    }
                }
            ]
        }
        game_flow = {
            "game_flow": [
                {
                    "hint_scene_images": {"puzzle_main": "frames/中文线索.jpg"},
                    "options": [{"image_path": "option_images/safe_option.jpg"}],
                }
            ]
        }

        validation = generate_unity_json.validate_generated_assets(str(temp_output), game_meta, game_flow)

        self.assertTrue(validation["ok"])
        self.assertEqual(validation["missing_assets"], [])
        self.assertIn("frames/中文线索.jpg", validation["non_ascii_assets"])

    def test_validate_generated_assets_fails_when_files_missing(self):
        temp_output = prepare_temp_dir("validate_assets_missing")
        (temp_output / "frames").mkdir(parents=True, exist_ok=True)
        (temp_output / "frames" / "scene_001.jpg").write_bytes(b"ok")

        game_meta = {
            "game_levels": [
                {
                    "hint_scene_images": {
                        "puzzle_main": "frames/scene_001.jpg",
                    }
                }
            ]
        }
        game_flow = {
            "game_flow": [
                {
                    "hint_scene_images": {"puzzle_main": "frames/缺失.jpg"},
                }
            ]
        }

        validation = generate_unity_json.validate_generated_assets(str(temp_output), game_meta, game_flow)

        self.assertFalse(validation["ok"])
        self.assertIn("frames/缺失.jpg", validation["missing_assets"])

    def test_generate_game_meta_flow_falls_back_when_embedding_model_unavailable(self):
        sample_input = Path("output") / "001" / "extracted_context.json"
        temp_output = prepare_temp_dir("unity_json_fallback")
        input_copy = temp_output / "extracted_context.json"
        sample_data = json.loads(sample_input.read_text(encoding="utf-8"))
        sample_data["source_video"] = "videos/001.mp4"
        input_copy.write_text(json.dumps(sample_data, ensure_ascii=False, indent=2), encoding="utf-8")

        with patch.object(generate_unity_json, "_load_embedding_model", return_value=None), \
             patch.object(generate_unity_json, "VolcEngineAIGCGenerator", _FakeImageGenerator), \
             patch.object(generate_unity_json, "_export_activity_audio_asset", return_value="media/audio/activity_theme_audio.mp3"), \
             patch.object(generate_unity_json, "_export_event_video_asset", side_effect=lambda **kwargs: f"media/video/{kwargs['event_id']}.mp4"), \
             patch.object(generate_unity_json, "_generate_distractor_option_with_llm", return_value=["apple", "bread", "drink"]), \
             patch.object(generate_unity_json, "_generate_distractor_events_with_llm", return_value={"event1": "distractor event"}):
            game_meta, game_flow = generate_unity_json.generate_game_meta_flow(
                input_path=str(input_copy),
                output_dir=str(temp_output),
                user_id="001",
            )

        self.assertIn("game_levels", game_meta)
        self.assertIn("game_flow", game_flow)
        self.assertTrue((temp_output / "GameMeta.json").exists())
        self.assertTrue((temp_output / "GameFlow.json").exists())

    def test_event_items_keeps_entities_without_coordinates(self):
        event = {
            "__idx": 1,
            "_entity_status": "done",
            "entities": [
                {
                    "id": "o1",
                    "item_name": "果汁",
                    "detail_pair": [
                        {
                            "question": "是什么口味？",
                            "correct_answer": "青柠味",
                            "options": ["原味", "青柠味"],
                        }
                    ],
                }
            ],
        }

        items = generate_unity_json._event_items(event)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_name"], "果汁")
        question, options, answer = generate_unity_json._extract_detail_question(items[0])
        self.assertEqual(question, "是什么口味？")
        self.assertEqual(answer, "青柠味")
        self.assertIn("青柠味", options)

    def test_confusion_tasks_reindex_following_stages(self):
        sample_input = Path("output") / "001" / "extracted_context.json"
        temp_output = prepare_temp_dir("unity_json_confusion")
        input_copy = temp_output / "extracted_context.json"
        sample_data = json.loads(sample_input.read_text(encoding="utf-8"))
        sample_data["source_video"] = "videos/001.mp4"
        input_copy.write_text(json.dumps(sample_data, ensure_ascii=False, indent=2), encoding="utf-8")

        with patch.object(generate_unity_json, "_load_embedding_model", return_value=None), \
             patch.object(generate_unity_json, "VolcEngineAIGCGenerator", _FakeImageGenerator), \
             patch.object(generate_unity_json, "_export_activity_audio_asset", return_value="media/audio/activity_theme_audio.mp3"), \
             patch.object(generate_unity_json, "_export_event_video_asset", side_effect=lambda **kwargs: f"media/video/{kwargs['event_id']}.mp4"), \
             patch.object(generate_unity_json, "_generate_distractor_option_with_llm", return_value=["apple", "bread", "drink"]), \
             patch.object(generate_unity_json, "_generate_distractor_events_with_llm", return_value={"event1": "distractor event"}), \
             patch.object(generate_unity_json, "_select_confusion_events", return_value=[{"event_name": "fake event", "image_path": "default/fake.jpg"}]), \
             patch("random.randint", return_value=1):
            _, game_flow = generate_unity_json.generate_game_meta_flow(
                input_path=str(input_copy),
                output_dir=str(temp_output),
                confusion_count=1,
                user_id="001",
            )

        stages = game_flow["game_flow"]
        stage_ids = [stage["stage_id"] for stage in stages]
        stage_indexes = [stage["stage_index"] for stage in stages]
        self.assertEqual(stage_ids, [f"stage_{i:03d}" for i in range(1, len(stages) + 1)])
        self.assertEqual(stage_indexes, list(range(1, len(stages) + 1)))
        self.assertTrue(any(stage.get("correct_answer") is False for stage in stages if stage.get("task_type") == "recall"))
