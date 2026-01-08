"""
火山引擎 SeedEdit v3.0 图生图 AIGC 线索生成器
用于生成间接视觉线索图像，支持本地图片或 URL 输入。
符合 GB 45438-2025 隐式标识要求（可选添加 AIGC 元数据）。
"""

import os
import time
import base64
import json
import logging
from typing import Any, Optional, Union
from urllib.parse import urlencode
import requests
from hashlib import sha256
import hmac

# 配置日志
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

    # 接口常量
    API_HOST = "https://visual.volcengineapi.com"
    REGION = "cn-north-1"
    SERVICE = "cv"
    VERSION = "2022-08-31"

    # 任务状态
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
        os.makedirs(self.output_dir, exist_ok=True)

    def _sign_request(
        self,
        method: str,
        host: str,
        path: str,
        query: dict,
        headers: dict,
        body: str = "",
    ) -> str:
        """
        根据火山引擎签名规则 V4 生成 Authorization 头。
        参考：https://www.volcengine.com/docs/6295/65473
        """
        algorithm = "HMAC-SHA256"
        service = self.SERVICE
        region = self.REGION
        date = headers["X-Date"][:8]  # YYYYMMDD

        # 规范请求
        canonical_uri = path
        canonical_querystring = "&".join([f"{k}={v}" for k, v in sorted(query.items())])
        canonical_headers = "\n".join([f"{k.lower()}:{v}" for k, v in sorted(headers.items())]) + "\n"
        signed_headers = ";".join([k.lower() for k in sorted(headers.keys())])
        payload_hash = sha256(body.encode("utf-8")).hexdigest()
        canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

        # 待签名字符串
        credential_scope = f"{date}/{region}/{service}/request"
        string_to_sign = f"{algorithm}\n{headers['X-Date']}\n{credential_scope}\n{sha256(canonical_request.encode('utf-8')).hexdigest()}"

        # 计算签名
        def sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), sha256).digest()

        k_date = sign(("HMAC" + self.sk).encode("utf-8"), date)
        k_region = sign(k_date, region)
        k_service = sign(k_region, service)
        k_signing = sign(k_service, "request")
        signature = sign(k_signing, string_to_sign).hex()

        return f"{algorithm} Credential={self.ak}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"

    def _build_headers(self, body: str = "") -> dict:
        """构建公共请求头"""
        x_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": "visual.volcengineapi.com",
            "X-Date": x_date,
        }
        if body:
            headers["Content-Length"] = str(len(body))
        return headers

    def _send_request(
        self,
        action: str,
        body_dict: dict,
        max_retries: int = MAX_RETRY,
    ) -> dict:
        """
        发送带签名的 POST 请求到火山引擎。

        :param action: Action 参数（如 CVSync2AsyncSubmitTask）
        :param body_dict: JSON body 内容
        :return: 响应 JSON 字典
        """
        query = {"Action": action, "Version": self.VERSION}
        url = f"{self.API_HOST}?{urlencode(query)}"
        body = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":"))

        for attempt in range(max_retries):
            try:
                headers = self._build_headers(body)
                headers["Authorization"] = self._sign_request("POST", "visual.volcengineapi.com", "/", query, headers, body)

                logger.debug(f"Sending request to {url}")
                resp = requests.post(url, headers=headers, data=body, timeout=10)
                resp.raise_for_status()
                result = resp.json()

                # 检查业务错误码
                if result.get("code") != 10000:
                    logger.error(f"VolcEngine API error: {result.get('message')} (code={result.get('code')})")
                    raise RuntimeError(f"API Error: {result.get('message')}")

                return result

            except Exception as e:
                logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)  # 指数退避

        raise RuntimeError("Max retries exceeded")

    def _read_image_as_base64(self, image_path: str) -> str:
        """读取本地图片并转为 base64"""
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Input image not found: {image_path}")
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _download_image(self, url: str, save_path: str) -> None:
        """下载图片到本地"""
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(resp.content)

    def generate_image(
        self,
        prompt: str,
        input_image_path: Optional[str] = None,
        image_url: Optional[str] = None,
        seed: int = -1,
        scale: float = 0.5,
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
            "prompt": prompt[:800],  # 截断超长 prompt
            "seed": seed,
            "scale": max(0.0, min(1.0, scale)),
        }

        if input_image_path:
            body_submit["binary_data_base64"] = [self._read_image_as_base64(input_image_path)]
        else:
            body_submit["image_urls"] = [image_url]

        logger.info(f"Submitting task with prompt: {prompt}")
        submit_resp = self._send_request("CVSync2AsyncSubmitTask", body_submit)
        task_id = submit_resp["data"]["task_id"]
        logger.info(f"Task submitted, task_id: {task_id}")

        # Step 2: 轮询查询结果
        start_time = time.time()
        req_json_config: dict[str, Any] ={
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
            # 可根据实际服务商信息填写
            req_json_config["aigc_meta"] = {
                "producer_id": "memory_assistant_v1",  # 示例 ID
                "content_producer": "Tongji CDI",
            }

        body_query = {
            "req_key": "seededit_v3.0",
            "task_id": task_id,
            "req_json": json.dumps(req_json_config, separators=(",", ":")),
        }

        while time.time() - start_time < self.TIMEOUT:
            try:
                query_resp = self._send_request("CVSync2AsyncGetResult", body_query)
                status = query_resp["data"]["status"]

                if status == self.STATUS_DONE:
                    image_urls = query_resp["data"].get("image_urls")
                    if not image_urls:
                        raise RuntimeError("No image returned from API")
                    output_url = image_urls[0]
                    logger.info(f"Image generated: {output_url}")

                    # 保存到本地
                    safe_prompt = "".join(c for c in prompt[:30] if c.isalnum() or c in " _-")
                    filename = f"clue_{task_id[:8]}_{safe_prompt}.jpg"
                    save_path = os.path.join(self.output_dir, filename)
                    self._download_image(output_url, save_path)
                    logger.info(f"Image saved to: {save_path}")
                    return save_path

                elif status in [self.STATUS_IN_QUEUE, self.STATUS_GENERATING]:
                    logger.info(f"Task status: {status}, polling again in {self.POLL_INTERVAL}s...")
                    time.sleep(self.POLL_INTERVAL)
                else:
                    raise RuntimeError(f"Unexpected task status: {status}")

            except Exception as e:
                logger.error(f"Error during polling: {e}")
                raise

        raise TimeoutError("Image generation timed out")