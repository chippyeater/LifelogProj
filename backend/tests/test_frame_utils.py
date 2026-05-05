import os
import unittest

from tests.test_support import clear_temp_root, prepare_temp_dir
from utils.frame_utils import extract_frame_to_path, parse_timestamp_to_seconds, timestamp_to_frame_index


class FrameUtilsTest(unittest.TestCase):
    # 测试公共抽帧工具的最小行为

    def tearDown(self):
        clear_temp_root()

    def test_parse_timestamp_to_seconds(self):
        self.assertEqual(parse_timestamp_to_seconds("00:00:06.500"), 6.5)
        self.assertEqual(parse_timestamp_to_seconds("01:05"), 65.0)
        self.assertEqual(parse_timestamp_to_seconds("7"), 7.0)

    def test_timestamp_to_frame_index(self):
        self.assertEqual(timestamp_to_frame_index("00:00:06.000", 30.0), 180)

    def test_extract_frame_to_path(self):
        out_dir = prepare_temp_dir("frame_utils")
        out_path = os.path.join(out_dir, "frame.jpg")
        ok = extract_frame_to_path("videos/001.mp4", "00:00:06.000", out_path)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(out_path))
