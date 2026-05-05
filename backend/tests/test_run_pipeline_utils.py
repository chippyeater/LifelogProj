import unittest
from pathlib import Path

from pipeline_cache import PipelineCacheManager
from db import get_pipeline_state, get_video_record, init_db
from tests.test_support import clear_temp_root, import_with_stubs, prepare_temp_dir


run_pipeline = import_with_stubs(
    "run_pipeline",
    {
        "cv2": {
            "VideoCapture": object,
            "CAP_PROP_FPS": 5,
            "CAP_PROP_FRAME_COUNT": 7,
        },
        "bailian_video_processor": {
            "create_bailian_processor": lambda *args, **kwargs: None,
        },
        "make_context": {
            "execute_entity_clue_enhancement": lambda *args, **kwargs: None,
            "execute_scene_clue_enhancement": lambda *args, **kwargs: None,
            "execute_video_processor_single_call": lambda *args, **kwargs: {},
            "extract_reference_frames": lambda *args, **kwargs: None,
        },
        "clue_aigc_generator": {
            "VolcEngineAIGCGenerator": object,
        },
        "generate_unity_json": {
            "generate_game_meta_flow": lambda *args, **kwargs: ({}, {}),
            "validate_generated_assets": lambda *args, **kwargs: {"ok": True, "asset_count": 0, "missing_assets": [], "non_ascii_assets": []},
        },
    },
)


class RunPipelineUtilsTest(unittest.TestCase):
    def tearDown(self):
        clear_temp_root()

    def test_format_duration(self):
        self.assertEqual(run_pipeline._format_duration(0), "00:00:00")
        self.assertEqual(run_pipeline._format_duration(65), "00:01:05")
        self.assertEqual(run_pipeline._format_duration(3661), "01:01:01")
        self.assertEqual(run_pipeline._format_duration(-5), "00:00:00")

    def test_parse_location(self):
        self.assertEqual(
            run_pipeline._parse_iso6709_location("+39.1234-120.5678/"),
            (39.1234, -120.5678),
        )
        self.assertEqual(
            run_pipeline._parse_lat_lon("lat=39.12 lon=120.56"),
            (39.12, 120.56),
        )
        self.assertEqual(
            run_pipeline._parse_lat_lon("39.12,120.56"),
            (39.12, 120.56),
        )
        self.assertIsNone(run_pipeline._parse_lat_lon("not-a-location"))

    def test_parse_datetime(self):
        self.assertIsNotNone(run_pipeline._parse_datetime("2026-04-11T12:30:00Z"))
        self.assertIsNotNone(run_pipeline._parse_datetime("2026-04-11 12:30:00"))
        self.assertIsNotNone(run_pipeline._parse_datetime("2026/04/11 12:30:00"))
        self.assertIsNone(run_pipeline._parse_datetime("bad-datetime"))

    def test_clear_image_paths(self):
        context = {
            "activity_visual_clue": [
                {
                    "reference_frame_path": "frames/a.jpg",
                    "enhanced_image_path": "enhanced/a.jpg",
                }
            ],
            "events": {
                "e1": {
                    "scene_clues": [
                        {
                            "reference_frame_path": "frames/b.jpg",
                            "enhanced_image_path": "enhanced/b.jpg",
                        }
                    ],
                    "key_persons": [
                        {
                            "reference_frame_path": "frames/c.jpg",
                            "enhanced_image_path": "enhanced/c.jpg",
                        }
                    ],
                    "key_objects": [
                        {
                            "reference_frame_path": "frames/d.jpg",
                            "enhanced_image_path": "enhanced/d.jpg",
                        }
                    ],
                }
            },
        }

        run_pipeline._clear_image_paths(context)

        clue = context["activity_visual_clue"][0]
        self.assertIsNone(clue["reference_frame_path"])
        self.assertIsNone(clue["enhanced_image_path"])

        event = context["events"]["e1"]
        self.assertIsNone(event["scene_clues"][0]["reference_frame_path"])
        self.assertIsNone(event["scene_clues"][0]["enhanced_image_path"])
        self.assertIsNone(event["key_persons"][0]["reference_frame_path"])
        self.assertIsNone(event["key_persons"][0]["enhanced_image_path"])
        self.assertIsNone(event["key_objects"][0]["reference_frame_path"])
        self.assertIsNone(event["key_objects"][0]["enhanced_image_path"])

    def test_resolve_stage_resume_files(self):
        user_dir = prepare_temp_dir("resume_stage_paths")
        cache_manager = PipelineCacheManager(str(user_dir))
        cache_manager.ensure_dirs()

        stage2_files = run_pipeline._resolve_stage_resume_files(cache_manager, 2)
        stage3_files = run_pipeline._resolve_stage_resume_files(cache_manager, 3)

        self.assertTrue(stage2_files["context_path"].endswith("stage1_video_parse\\stage1_timeline_output.json"))
        self.assertTrue(stage3_files["context_path"].endswith("stage2_event_rebuild\\stage2_event_rebuild_output.json"))

    def test_validate_resume_files(self):
        user_dir = prepare_temp_dir("resume_stage_validate")
        cache_manager = PipelineCacheManager(str(user_dir))
        cache_manager.ensure_dirs()
        file_path = cache_manager.stage_file("stage1_video_parse", "video_parse_output.json")
        Path(file_path).write_text("{}", encoding="utf-8")

        run_pipeline._validate_resume_files({"context_path": file_path})

        with self.assertRaises(FileNotFoundError):
            run_pipeline._validate_resume_files({"context_path": str(Path(user_dir) / "missing.json")})

    def test_should_stop_after_stage(self):
        self.assertTrue(run_pipeline._should_stop_after_stage(1, 1))
        self.assertTrue(run_pipeline._should_stop_after_stage(2, 2))
        self.assertTrue(run_pipeline._should_stop_after_stage(3, 3))
        self.assertFalse(run_pipeline._should_stop_after_stage(1, None))
        self.assertFalse(run_pipeline._should_stop_after_stage(1, 2))

    def test_validate_pipeline_mode_args_accepts_valid_combinations(self):
        run_pipeline._validate_pipeline_mode_args("staged", 1, None)
        run_pipeline._validate_pipeline_mode_args("staged", 2, 3)
        run_pipeline._validate_pipeline_mode_args("full_context", 1, None)

    def test_validate_pipeline_mode_args_rejects_full_context_stage_controls(self):
        with self.assertRaises(ValueError):
            run_pipeline._validate_pipeline_mode_args("full_context", 2, None)
        with self.assertRaises(ValueError):
            run_pipeline._validate_pipeline_mode_args("full_context", 1, 2)

    def test_merge_pipeline_state_updates_incrementally(self):
        user_dir = prepare_temp_dir("pipeline_state_merge")
        db_path = str(user_dir / "lifelog.db")
        init_db(db_path)

        state = run_pipeline._merge_pipeline_state(
            db_path,
            "001",
            "001.mp4",
            events="done",
            last_error=None,
        )
        self.assertEqual(state["events"], "done")
        self.assertEqual(state["entities"], "pending")

        state = run_pipeline._merge_pipeline_state(
            db_path,
            "001",
            "001.mp4",
            unity="done",
        )
        self.assertEqual(state["events"], "done")
        self.assertEqual(state["unity"], "done")

        saved = get_pipeline_state(db_path, "001", "001.mp4")
        self.assertEqual(saved["events"], "done")
        self.assertEqual(saved["unity"], "done")

    def test_mark_pipeline_failure_persists_error_and_failed_status(self):
        user_dir = prepare_temp_dir("pipeline_state_failure")
        db_path = str(user_dir / "lifelog.db")
        init_db(db_path)

        state = run_pipeline._mark_pipeline_failure(
            db_path,
            "001",
            "001.mp4",
            "unity",
            RuntimeError("export failed"),
        )

        self.assertEqual(state["unity"], "failed")
        self.assertEqual(state["last_error"], "export failed")

        saved_state = get_pipeline_state(db_path, "001", "001.mp4")
        self.assertEqual(saved_state["unity"], "failed")
        self.assertEqual(saved_state["last_error"], "export failed")

        record = get_video_record(db_path, "001", "001.mp4")
        self.assertEqual(record["status"], "failed")
