import json
import unittest
from pathlib import Path

from pipeline_cache import PipelineCacheManager
from tests.test_support import clear_temp_root, prepare_temp_dir


class PipelineCacheTest(unittest.TestCase):
    # 测试阶段缓存骨架的目录和 manifest 是否能正确生成

    def tearDown(self):
        clear_temp_root()

    def test_cache_manager_creates_stage_dirs_and_manifest(self):
        user_dir = prepare_temp_dir("pipeline_cache_case")
        manager = PipelineCacheManager(str(user_dir))
        manager.ensure_dirs()

        for stage_name in manager.STAGE_ORDER:
            self.assertTrue((user_dir / "pipeline_cache" / stage_name).exists())

        manifest = manager.build_manifest(
            user_id="001",
            video_name="001.mp4",
            source_video="videos/001.mp4",
        )
        manager.save_manifest(manifest)

        manifest_path = user_dir / "pipeline_cache" / "manifest.json"
        self.assertTrue(manifest_path.exists())

        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_manifest["user_id"], "001")
        self.assertEqual(saved_manifest["video_name"], "001.mp4")
        self.assertIn("stage1_video_parse", saved_manifest["stages"])
        self.assertIn("stage3_detail_generate", saved_manifest["stages"])

    def test_mark_stage_updates_manifest_file_list(self):
        user_dir = prepare_temp_dir("pipeline_cache_mark_stage")
        manager = PipelineCacheManager(str(user_dir))
        manager.ensure_dirs()

        manifest = manager.build_manifest(
            user_id="003",
            video_name="003.mp4",
            source_video="videos/003.mp4",
        )
        json_path = manager.write_json(
            "stage1_video_parse",
            "video_parse_output.json",
            {"ok": True},
        )
        manager.mark_stage(
            manifest,
            "stage1_video_parse",
            status="done",
            files=[json_path],
        )
        manager.save_manifest(manifest)

        saved_manifest = json.loads((user_dir / "pipeline_cache" / "manifest.json").read_text(encoding="utf-8"))
        stage_info = saved_manifest["stages"]["stage1_video_parse"]
        self.assertEqual(stage_info["status"], "done")
        self.assertEqual(stage_info["files"], ["pipeline_cache/stage1_video_parse/video_parse_output.json"])
