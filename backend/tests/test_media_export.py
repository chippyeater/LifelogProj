import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_support import import_with_stubs


media_export = import_with_stubs(
    "utils.media_export",
    {
        "cv2": {
            "VideoCapture": object,
        },
        "utils.frame_utils": {
            "parse_timestamp_to_seconds": lambda value: (
                sum(float(part) * factor for part, factor in zip(value.replace(".", ":").split(":"), [3600, 60, 1, 0.001]))
                if value
                else 0.0
            ),
        },
    },
)


class MediaExportTest(unittest.TestCase):
    def test_resolve_export_base_url_prefers_explicit_value(self):
        with patch.dict(os.environ, {"API_BASE_URL": "http://env.example"}, clear=False):
            self.assertEqual(
                media_export.resolve_export_base_url("http://explicit.example/"),
                "http://explicit.example",
            )

    def test_resolve_export_base_url_falls_back_to_env(self):
        with patch.dict(os.environ, {"API_BASE_URL": "http://env.example/"}, clear=False):
            self.assertEqual(
                media_export.resolve_export_base_url(None),
                "http://env.example",
            )

    def test_build_asset_url(self):
        self.assertEqual(
            media_export.build_asset_url("http://host", "005", "media/video/e_001.mp4"),
            "http://host/assets/005/media/video/e_001.mp4",
        )

    def test_build_asset_relative_path(self):
        self.assertEqual(
            media_export.build_asset_relative_path("005", "media/video/e_001.mp4"),
            "media/video/e_001.mp4",
        )

    def test_clamp_media_range_extends_short_clip(self):
        start_time, end_time = media_export.clamp_media_range(
            "00:00:01.000",
            "00:00:02.000",
            min_seconds=4,
            max_seconds=10,
        )
        self.assertEqual(start_time, "00:00:00.000")
        self.assertEqual(end_time, "00:00:04.000")

    def test_clamp_media_range_trims_long_clip(self):
        start_time, end_time = media_export.clamp_media_range(
            "00:00:00.000",
            "00:00:20.000",
            min_seconds=4,
            max_seconds=10,
        )
        self.assertEqual(start_time, "00:00:05.000")
        self.assertEqual(end_time, "00:00:15.000")

    def test_export_video_clip_uses_unity_compatible_h264_settings(self):
        captured = []

        def _fake_run(args, check, capture_output, text):
            captured.append(args)
            if args[0] == "ffprobe":
                return SimpleNamespace(
                    stdout='{"streams":[{"codec_type":"video","codec_name":"h264","pix_fmt":"yuv420p","profile":"High"},{"codec_type":"audio","codec_name":"aac"}]}'
                )
            return SimpleNamespace(stdout="")

        with patch("utils.media_export.subprocess.run", side_effect=_fake_run), \
             patch("utils.media_export.os.makedirs"):
            media_export.export_video_clip("input.mp4", "00:00:01.000", "00:00:05.000", "out.mp4")

        ffmpeg_args = captured[0]
        self.assertIn("-pix_fmt", ffmpeg_args)
        self.assertIn("yuv420p", ffmpeg_args)
        self.assertIn("-profile:v", ffmpeg_args)
        self.assertIn("high", ffmpeg_args)
        self.assertIn("-level:v", ffmpeg_args)
        self.assertIn("4.0", ffmpeg_args)

    def test_validate_exported_video_clip_rejects_high10_output(self):
        with patch(
            "utils.media_export.subprocess.run",
            return_value=SimpleNamespace(
                stdout='{"streams":[{"codec_type":"video","codec_name":"h264","pix_fmt":"yuv420p10le","profile":"High 10"},{"codec_type":"audio","codec_name":"aac"}]}'
            ),
        ):
            with self.assertRaises(RuntimeError):
                media_export._validate_exported_video_clip("bad.mp4")
