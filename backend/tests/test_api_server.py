import unittest
import json
import os
import shutil
from unittest.mock import patch

from tests.test_support import import_with_stubs


class _FakeRequestArgs(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeRequest:
    def __init__(self):
        self.args = _FakeRequestArgs()
        self.host_url = "http://localhost:8000/"
        self._json = None

    def get_json(self, silent=False):
        return self._json


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def get_json(self):
        return self._payload


class _FakeFlask:
    def __init__(self, name):
        self.name = name
        self._routes = {}

    def before_request(self, func):
        self._before_request = func
        return func

    def get(self, path):
        def decorator(func):
            self._routes[("GET", path)] = func
            return func
        return decorator

    def post(self, path):
        def decorator(func):
            self._routes[("POST", path)] = func
            return func
        return decorator

    def test_client(self):
        return _FakeClient(self)


class _FakeClient:
    def __init__(self, app):
        self.app = app

    def get(self, path):
        route, _, query = path.partition("?")
        api_server.request.args = _FakeRequestArgs()
        api_server.request.host_url = "http://localhost:8000/"
        api_server.request._json = None
        if query:
            for pair in query.split("&"):
                if not pair:
                    continue
                key, _, value = pair.partition("=")
                api_server.request.args[key] = value
        if hasattr(self.app, "_before_request"):
            self.app._before_request()
        result = self.app._routes[("GET", route)]()
        if isinstance(result, tuple):
            payload, status_code = result
            return _FakeResponse(payload.get_json(), status_code)
        return _FakeResponse(result.get_json(), 200)

    def post(self, path, json=None):
        route, _, query = path.partition("?")
        api_server.request.args = _FakeRequestArgs()
        api_server.request.host_url = "http://localhost:8000/"
        api_server.request._json = json
        if query:
            for pair in query.split("&"):
                if not pair:
                    continue
                key, _, value = pair.partition("=")
                api_server.request.args[key] = value
        if hasattr(self.app, "_before_request"):
            self.app._before_request()
        result = self.app._routes[("POST", route)]()
        if isinstance(result, tuple):
            payload, status_code = result
            return _FakeResponse(payload.get_json(), status_code)
        return _FakeResponse(result.get_json(), 200)


def _fake_jsonify(payload):
    return _FakeResponse(payload)


def _fake_response(payload, status=200, content_type=None):
    parsed = json.loads(payload) if isinstance(payload, str) else payload
    response = _FakeResponse(parsed, status)
    if content_type:
        response.headers["Content-Type"] = content_type
    return response


def _fake_send_from_directory(*args, **kwargs):
    raise NotImplementedError


def _fake_abort(*args, **kwargs):
    raise RuntimeError("abort should not be called in this test")


api_server = import_with_stubs(
    "api_server",
    {
        "flask": {
            "Flask": _FakeFlask,
            "Response": _fake_response,
            "jsonify": _fake_jsonify,
            "send_from_directory": _fake_send_from_directory,
            "request": _FakeRequest(),
            "abort": _fake_abort,
        },
        "generate_unity_json": {
            "generate_game_meta_flow": lambda *args, **kwargs: ({}, {}),
            "validate_generated_assets": lambda *args, **kwargs: {"ok": True, "asset_count": 0, "missing_assets": [], "non_ascii_assets": []},
        },
    },
)


class ApiServerTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def test_get_users_returns_basic_user_list(self):
        users = [
            {
                "id": "001",
                "name": "Alice",
                "status": "processing",
                "updated_at": "2026-05-11T10:00:00Z",
            },
            {
                "id": "002",
                "name": "Bob",
                "status": "all_ready",
                "updated_at": "2026-05-11T09:00:00Z",
            },
        ]

        with patch.object(api_server, "list_users", return_value=users):
            response = self.client.get("/api/users")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["users"], users)

    def test_job_status_prefers_status_json(self):
        tmp_root = self._tmp_dir()
        user_dir = os.path.join(tmp_root, "001")
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ok": True,
                    "user": "001",
                    "task_id": "task_json",
                    "job_id": "task_json",
                    "video": "from_status.mp4",
                    "status": "processing",
                    "ready": False,
                    "progress": 65,
                    "current_step": "开始生成 AIGC 增强图",
                    "error": None,
                    "pipeline_state": {
                        "events": "done",
                        "entities": "done",
                        "frames": "done",
                        "aigc": "pending",
                        "unity": "pending",
                        "last_error": None,
                    },
                    "updated_at": "2026-05-06T10:00:00Z",
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        with patch.object(api_server, "OUTPUT_ROOT", tmp_root):
            response = self.client.get("/api/job-status?user=001")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["task_id"], "task_json")
        self.assertEqual(data["job_id"], "task_json")
        self.assertEqual(data["video"], "from_status.mp4")
        self.assertEqual(data["progress"], 65)
        self.assertEqual(data["current_step"], "开始生成 AIGC 增强图")
        self.assertEqual(data["pipeline_state"]["frames"], "done")

    def test_job_status_uses_latest_record_when_video_missing(self):
        record = {
            "video_name": "001.mp4",
            "status": "context_extracted",
            "video_url": "",
            "extracted_context_path": "output/001/extracted_context.json",
            "gamemeta_path": "",
            "gameflow_path": "",
            "subevent_count": 2,
            "processed_at": None,
            "updated_at": "2026-04-22T12:00:01+00:00",
        }
        pipeline_state = {
            "events": "done",
            "entities": "done",
            "frames": "done",
            "aigc": "pending",
            "unity": "pending",
            "last_error": None,
        }

        with patch.object(api_server, "get_latest_video_record", return_value=record), \
             patch.object(api_server, "get_pipeline_state", return_value=pipeline_state):
            response = self.client.get("/api/job-status?user=001")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["video"], "001.mp4")
        self.assertEqual(data["status"], "context_extracted")
        self.assertEqual(data["progress"], 60)
        self.assertFalse(data["ready"])

    def test_job_status_returns_ready_payload(self):
        record = {
            "status": "all_ready",
            "video_url": "https://example.com/video.mp4",
            "extracted_context_path": "output/001/extracted_context.json",
            "gamemeta_path": "output/001/GameMeta.json",
            "gameflow_path": "output/001/GameFlow.json",
            "subevent_count": 3,
            "processed_at": "2026-04-22T12:00:00+00:00",
            "updated_at": "2026-04-22T12:00:01+00:00",
        }
        pipeline_state = {
            "events": "done",
            "entities": "done",
            "frames": "done",
            "aigc": "done",
            "unity": "done",
            "last_error": None,
        }

        with patch.object(api_server, "get_video_record", return_value=record), \
             patch.object(api_server, "get_pipeline_state", return_value=pipeline_state), \
             patch.object(api_server, "_build_asset_validation", return_value={"ok": True, "asset_count": 4, "missing_assets": [], "non_ascii_assets": []}):
            response = self.client.get("/api/job-status?user=001&video=001.mp4")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["ready"])
        self.assertEqual(data["status"], "all_ready")
        self.assertEqual(data["progress"], 100)
        self.assertEqual(data["video"], "001.mp4")
        self.assertEqual(data["pipeline_state"]["unity"], "done")
        self.assertEqual(data["asset_validation"]["asset_count"], 4)
        self.assertTrue(data["game_meta_url"].endswith("/api/game-meta?user=001"))
        self.assertTrue(data["game_flow_url"].endswith("/api/game-flow?user=001"))

    def test_job_status_blocks_ready_when_assets_invalid(self):
        record = {
            "status": "all_ready",
            "video_url": "https://example.com/video.mp4",
            "extracted_context_path": "output/001/extracted_context.json",
            "gamemeta_path": "output/001/GameMeta.json",
            "gameflow_path": "output/001/GameFlow.json",
            "subevent_count": 3,
            "processed_at": "2026-04-22T12:00:00+00:00",
            "updated_at": "2026-04-22T12:00:01+00:00",
        }
        pipeline_state = {
            "events": "done",
            "entities": "done",
            "frames": "done",
            "aigc": "done",
            "unity": "done",
            "last_error": None,
        }

        invalid_assets = {
            "ok": False,
            "asset_count": 4,
            "missing_assets": ["frames/missing.jpg"],
            "non_ascii_assets": [],
        }
        with patch.object(api_server, "get_video_record", return_value=record), \
             patch.object(api_server, "get_pipeline_state", return_value=pipeline_state), \
             patch.object(api_server, "_build_asset_validation", return_value=invalid_assets):
            response = self.client.get("/api/job-status?user=001&video=001.mp4")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["ready"])
        self.assertEqual(data["asset_validation"]["missing_assets"], ["frames/missing.jpg"])

    def test_post_and_get_recall_report(self):
        payload = {
            "report_version": 1,
            "task_context": {"foo": "bar"},
            "user_performance": {"error_trial": 2},
        }

        with patch.object(api_server, "OUTPUT_ROOT", self._tmp_dir()):
            post_response = self.client.post("/api/recall-report?user=001", json=payload)
            self.assertEqual(post_response.status_code, 200)
            self.assertTrue(post_response.get_json()["ok"])

            get_response = self.client.get("/api/recall-report?user=001")
            self.assertEqual(get_response.status_code, 200)
            self.assertEqual(get_response.get_json()["task_context"]["foo"], "bar")

    def _tmp_dir(self):
        path = os.path.join(os.getcwd(), ".tmp_test_api_server")
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)
        return path


if __name__ == "__main__":
    unittest.main()
