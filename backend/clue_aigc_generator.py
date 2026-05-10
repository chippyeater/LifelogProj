"""
火山引擎 SeedEdit v3.0 图生图 AIGC 线索生成器
用于生成间接视觉线索图像，支持本地图片或 URL 输入。
符合 GB 45438-2025 隐式标识要求（可选添加 AIGC 元数据）。
"""

import os
import re
import time
import base64
import json
import logging
import ast
import random
from typing import Any, Optional
import requests
import cv2
from volcengine.visual.VisualService import VisualService
from runtime_config import get_config_value

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class VolcEngineAIGCGenerator:
    """
    使用火山引擎 CVSync2AsyncSubmitTask / CVSync2AsyncGetResult 接口
    调用 SeedEdit v3.0 模型生成编辑后的图像。
    """

    # Task status
    STATUS_DONE = "done"
    STATUS_GENERATING = "generating"
    STATUS_IN_QUEUE = "in_queue"
    MAX_RETRY = 3
    POLL_INTERVAL = 3  # 秒
    TIMEOUT = 30  # 总超时时间（秒）

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        output_dir: str = "user_data/visual_clues",
        add_aigc_meta: bool = True,
        add_logo: bool = False,
    ):
        """
        初始化生成器。

        :param access_key: 火山引擎 AK
        :param secret_key: 火山引擎 SK
        :param output_dir: 生成图像保存目录
        :param add_aigc_meta: 是否添加 AIGC 隐式标识元数据（符合国标）
        :param add_logo: 是否添加明水印（默认不加）
        """
        self.ak = access_key
        self.sk = secret_key
        self.output_dir = output_dir
        self.add_aigc_meta = add_aigc_meta
        self.add_logo = add_logo
        aigc_cfg = get_config_value("aigc", {})
        self.max_retry = int(aigc_cfg.get("max_retry", self.MAX_RETRY))
        self.poll_interval = float(aigc_cfg.get("poll_interval_seconds", self.POLL_INTERVAL))
        self.timeout_seconds = float(aigc_cfg.get("timeout_seconds", self.TIMEOUT))
        self.download_timeout_seconds = float(aigc_cfg.get("download_timeout_seconds", 10))
        self.prompt_max_chars = int(aigc_cfg.get("prompt_max_chars", 800))
        os.makedirs(self.output_dir, exist_ok=True)

        # Initialize CV SDK client
        self.visual_service = VisualService()

        self.visual_service.set_ak(self.ak)
        self.visual_service.set_sk(self.sk)

    def _normalize_path(self, path: str) -> str:
        # Normalize to POSIX-style for JSON outputs and frontend compatibility
        return os.path.normpath(path).replace("\\", "/")

    def _send_sdk_request(
        self,
        action: str,
        body_dict: dict,
        max_retries: int | None = None,
    ) -> dict:
        """
        发送带签名的 POST 请求到火山引擎。

        :param action: Action 参数（如 CVSync2AsyncSubmitTask）
        :param body_dict: JSON body 内容
        :return: 响应 JSON 字典
        """
        def _parse_json_payload(payload: Any) -> dict:
            if isinstance(payload, dict):
                return payload
            if isinstance(payload, bytes):
                text = payload.decode("utf-8", errors="replace")
            elif isinstance(payload, str):
                text = payload
            else:
                text = str(payload)
            text = text.strip()
            if not text:
                raise ValueError("Empty response")
            return json.loads(text)

        def _try_parse_error(err: Exception | str | bytes) -> Optional[dict]:
            if isinstance(err, bytes):
                try:
                    return _parse_json_payload(err)
                except Exception:
                    return None
            err_str = err if isinstance(err, str) else str(err)
            err_str = err_str.strip()
            if err_str.startswith("b'") or err_str.startswith('b"'):
                try:
                    raw_bytes = ast.literal_eval(err_str)
                    return _parse_json_payload(raw_bytes)
                except Exception:
                    return None
            return None

        retry_total = self.max_retry if max_retries is None else max_retries
        for attempt in range(retry_total):
            try:
                logger.debug(f"SDK request - Action: {action}, Body: {body_dict}")
                resp = self.visual_service.common_json_handler(action, body_dict)
                result = _parse_json_payload(resp)

                # 检查业务错误码
                if result.get("code") != 10000:
                    code = result.get("code")
                    message = result.get("message")
                    logger.error(f"VolcEngine API error: {message} (code={code})")
                    raise RuntimeError(f"API Error: {message} (code={code})")

                return result

            except Exception as e:
                parsed = _try_parse_error(e)
                if parsed:
                    code = parsed.get("code")
                    message = parsed.get("message")
                    err_msg = f"{message} (code={code})"
                else:
                    code = None
                    err_msg = str(e)

                logger.warning(
                    f"SDK request failed (attempt {attempt + 1}/{retry_total}): {err_msg}"
                )
                if attempt == retry_total - 1:
                    raise RuntimeError(err_msg)

                delay = 2 ** attempt
                if code == 50430 or "Concurrent Limit" in err_msg:
                    delay = max(delay, 5) + random.random()
                time.sleep(delay)

        raise RuntimeError("Max retries exceeded")

    def _read_image_as_base64(self, image_path: str) -> str:
        """读取本地图片并转为 base64"""
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Input image not found: {image_path}")
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _download_image(self, url: str, save_path: str) -> None:
        """下载图片到本地"""
        resp = requests.get(url, timeout=self.download_timeout_seconds)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(resp.content)

    def _extract_prompt_field(self, prompt: str, key: str) -> str:
        idx = prompt.find(key)
        if idx == -1:
            return ""
        start = idx + len(key)
        end = prompt.find("。", start)
        if end == -1:
            end = prompt.find(".", start)
        if end == -1:
            end = len(prompt)
        return prompt[start:end].strip()

    def _sanitize_filename_component(self, text: str, max_len: int = 6) -> str:
        if not text:
            return "unknown"
        cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in text)
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        return (cleaned or "unknown")[:max_len]


    def generate_image(
        self,
        prompt: str,
        input_image_path: Optional[str] = None,
        image_url: Optional[str] = None,
        seed: int = -1,
        scale: float = 0.8,
        output_size: Optional[tuple[int, int]] = None,
    ) -> str:
        """
        生成编辑后的图像。

        :param prompt: 编辑提示词（建议 <= 120 字符）
        :param input_image_path: 本地输入图片路径（二选一）
        :param image_url: 远程图片 URL（二选一）
        :param seed: 随机种子
        :param scale: 文本影响强度 [0, 1]
        :return: 生成图像的本地保存路径
        """
        if not (input_image_path or image_url):
            raise ValueError("Must provide either input_image_path or image_url")
        if input_image_path and image_url:
            raise ValueError("Cannot provide both input_image_path and image_url")

        # Step 1: 提交任务
        body_submit = {
            "req_key": "seededit_v3.0",
            "prompt": prompt[: self.prompt_max_chars],
            "seed": seed,
            "scale": max(0.0, min(1.0, scale)),
        }

        if input_image_path:
            body_submit["binary_data_base64"] = [self._read_image_as_base64(input_image_path)]
        else:
            body_submit["image_urls"] = [image_url]

        logger.info(f"Submitting task with prompt: {prompt}")
        submit_resp = self._send_sdk_request("CVSync2AsyncSubmitTask", body_submit)
        task_id = submit_resp["data"]["task_id"]
        logger.info(f"Task submitted, task_id: {task_id}")

        # Step 2: 轮询查询结果
        start_time = time.time()
        req_json_config: dict[str, Any] = {
            "return_url": True,
        }
        if self.add_logo:
            req_json_config["logo_info"] = {
                "add_logo": True,
                "position": 0,  # 右下角
                "language": 0,  # 中文
                "opacity": 1.0,
            }
        if self.add_aigc_meta:
            req_json_config["aigc_meta"] = {
                "producer_id": "memory_assistant_v1",
                "content_producer": "Tongji CDI",
            }

        body_query = {
            "req_key": "seededit_v3.0",
            "task_id": task_id,
            "req_json": json.dumps(req_json_config, separators=(",", ":")),
        }

        while time.time() - start_time < self.timeout_seconds:
            try:
                query_resp = self._send_sdk_request("CVSync2AsyncGetResult", body_query)
                status = query_resp["data"]["status"]

                if status == self.STATUS_DONE:
                    image_urls = query_resp["data"].get("image_urls")
                    if not image_urls:
                        raise RuntimeError("No image returned from API")
                    output_url = image_urls[0]
                    logger.info(f"Image generated: {output_url}")

                    activity = self._extract_prompt_field(prompt, "活动背景：")
                    scene = self._extract_prompt_field(prompt, "当前场景：")
                    target = (
                        self._extract_prompt_field(prompt, "目标人物：")
                        or self._extract_prompt_field(prompt, "目标物品：")
                        or self._extract_prompt_field(prompt, "目标对象：")
                    )
                    activity = self._sanitize_filename_component(activity)
                    scene = self._sanitize_filename_component(scene)
                    target = self._sanitize_filename_component(target)
                    filename = f"clue_{activity}_{scene}_{target}.jpg"
                    save_path = os.path.join(self.output_dir, filename)
                    self._download_image(output_url, save_path)
                    if output_size:
                        try:
                            img = cv2.imread(save_path)
                            if img is not None:
                                resized = cv2.resize(img, output_size, interpolation=cv2.INTER_AREA)
                                cv2.imwrite(save_path, resized)
                        except Exception as e:
                            logger.warning(f"Failed to resize image to {output_size}: {e}")
                    normalized_path = self._normalize_path(save_path)
                    logger.info(f"Image saved to: {normalized_path}")
                    return normalized_path

                if status in [self.STATUS_IN_QUEUE, self.STATUS_GENERATING]:
                    logger.info(f"Task status: {status}, polling again in {self.poll_interval}s...")
                    time.sleep(self.poll_interval)
                else:
                    raise RuntimeError(f"Unexpected task status: {status}")

            except Exception as e:
                logger.error(f"Error during polling: {e}")
                raise

        raise TimeoutError("Image generation timed out")

    def generate_image_text(
        self,
        prompt: str,
        *,
        seed: int = -1,
        scale: float = 2.5,
        width: int = 512,
        height: int = 512,
        use_pre_llm: bool = False,
        output_size: Optional[tuple[int, int]] = None,
    ) -> str:
        """
        文生图（high_aes_general_v30l_zt2i）
        """
        body_submit = {
            "req_key": "high_aes_general_v30l_zt2i",
            "prompt": prompt,
            "seed": seed,
            "scale": float(scale),
            "width": int(width),
            "height": int(height),
            "use_pre_llm": bool(use_pre_llm),
        }

        logger.info("Submitting text2img task with prompt: %s", prompt)
        submit_resp = self._send_sdk_request("CVSync2AsyncSubmitTask", body_submit)
        task_id = submit_resp["data"]["task_id"]
        logger.info("Text2img task submitted, task_id: %s", task_id)

        start_time = time.time()
        req_json_config: dict[str, Any] = {
            "return_url": True,
        }
        if self.add_logo:
            req_json_config["logo_info"] = {
                "add_logo": True,
                "position": 0,
                "language": 0,
                "opacity": 1.0,
            }
        if self.add_aigc_meta:
            req_json_config["aigc_meta"] = {
                "producer_id": "memory_assistant_v1",
                "content_producer": "Tongji CDI",
            }

        body_query = {
            "req_key": "high_aes_general_v30l_zt2i",
            "task_id": task_id,
            "req_json": json.dumps(req_json_config, separators=(",", ":")),
        }

        while time.time() - start_time < self.timeout_seconds:
            query_resp = self._send_sdk_request("CVSync2AsyncGetResult", body_query)
            status = query_resp["data"]["status"]

            if status == self.STATUS_DONE:
                image_urls = query_resp["data"].get("image_urls")
                if not image_urls:
                    raise RuntimeError("No image returned from API")
                output_url = image_urls[0]
                logger.info("Text2img image generated: %s", output_url)

                filename = f"txt2img_{int(time.time())}_{random.randint(1000,9999)}.jpg"
                save_path = os.path.join(self.output_dir, filename)
                self._download_image(output_url, save_path)
                if output_size:
                    try:
                        img = cv2.imread(save_path)
                        if img is not None:
                            resized = cv2.resize(img, output_size, interpolation=cv2.INTER_AREA)
                            cv2.imwrite(save_path, resized)
                    except Exception as e:
                        logger.warning("Failed to resize image to %s: %s", output_size, e)
                normalized_path = self._normalize_path(save_path)
                logger.info("Image saved to: %s", normalized_path)
                return normalized_path

            if status in [self.STATUS_IN_QUEUE, self.STATUS_GENERATING]:
                logger.info("Task status: %s, polling again in %ss...", status, self.poll_interval)
                time.sleep(self.poll_interval)
            else:
                raise RuntimeError(f"Unexpected task status: {status}")

        raise TimeoutError("Text2img generation timed out")
