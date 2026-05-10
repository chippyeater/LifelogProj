import json
import os
from typing import Any, Dict

from runtime_config import get_config_value


class PipelineCacheManager:
    # 统一管理分阶段缓存目录和文件写入
    STAGE_ORDER = [
        "stage1_video_parse",
        "stage2_event_rebuild",
        "stage3_detail_generate",
    ]

    def __init__(self, user_dir: str) -> None:
        self.user_dir = user_dir
        cache_dirname = str(get_config_value("paths.pipeline_cache_dirname", "pipeline_cache"))
        self.cache_root = os.path.join(user_dir, cache_dirname)
        self.manifest_path = os.path.join(self.cache_root, "manifest.json")

    def ensure_dirs(self) -> None:
        os.makedirs(self.cache_root, exist_ok=True)
        for stage_name in self.STAGE_ORDER:
            os.makedirs(self.stage_dir(stage_name), exist_ok=True)

    def stage_dir(self, stage_name: str) -> str:
        return os.path.join(self.cache_root, stage_name)

    def stage_file(self, stage_name: str, filename: str) -> str:
        return os.path.join(self.stage_dir(stage_name), filename)

    def write_json(self, stage_name: str, filename: str, payload: Any) -> str:
        stage_dir = self.stage_dir(stage_name)
        os.makedirs(stage_dir, exist_ok=True)
        out_path = os.path.join(stage_dir, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return out_path

    def build_manifest(self, *, user_id: str, video_name: str, source_video: str) -> Dict[str, Any]:
        manifest = {
            "user_id": user_id,
            "video_name": video_name,
            "source_video": source_video.replace("\\", "/"),
            "cache_root": self.cache_root.replace("\\", "/"),
            "stages": {},
        }
        for stage_name in self.STAGE_ORDER:
            manifest["stages"][stage_name] = {
                "status": "pending",
                "files": [],
            }
        return manifest

    def save_manifest(self, manifest: Dict[str, Any]) -> None:
        os.makedirs(self.cache_root, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    def mark_stage(
        self,
        manifest: Dict[str, Any],
        stage_name: str,
        *,
        status: str,
        files: list[str] | None = None,
    ) -> None:
        stage_info = manifest.setdefault("stages", {}).setdefault(stage_name, {})
        stage_info["status"] = status
        if files is not None:
            stage_info["files"] = [self._to_rel_path(p) for p in files]

    def _to_rel_path(self, path: str) -> str:
        return os.path.relpath(path, self.user_dir).replace("\\", "/")
