# Python 3.12.4
"""
瑙嗛鐞嗚В妯″潡锛氬皝瑁呬簡瀵?TwelveLabs 宸ュ叿鐨勪袱娆¤皟鐢細

- analyze_events(ctx: ActivityContext)锛氫粠鏁存瑙嗛鎶藉嚭浜嬩欢鍒楄〃
- analyze_entities(ctx: EntityContext, event_id, start_ts, end_ts, ...)锛氬鏌愪釜浜嬩欢鏃堕棿娈垫娊鍑哄疄浣擄紙浜?鐗╋級鍙婄嚎绱?
"""
import json
import os
import time
import datetime
import logging
import inspect
from typing import Any, Dict

from jinja2 import Template
from twelvelabs import ResponseFormat, IndexesCreateRequestModelsItem, TwelveLabs
from twelvelabs.errors import BadRequestError, TooManyRequestsError
try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None
try:
    import httpcore  # type: ignore
except Exception:  # pragma: no cover
    httpcore = None

from my_basics import ActivityContext, EntityContext, parse_json_from_llm

logger = logging.getLogger(__name__)



def _looks_truncated_or_garbage(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    if not raw:
        return False
    zeros = raw.count("0")
    if zeros >= 200 and zeros / max(1, len(raw)) > 0.3:
        return True
    if " 0 0 0 0 0 0 0" in raw or "0000000000" in raw:
        return True
    return False


def create_index(
    client,
    index_name: str,
    model_name: str = "pegasus1.2",
    model_options: list[str] | None = None,
) -> str:
    if model_options is None:
        model_options = ["visual", "audio"]
    try:
        index = client.indexes.create(
            index_name=index_name,
            models=[
                IndexesCreateRequestModelsItem(
                    model_name=model_name,
                    model_options=model_options,
                ),
            ],
        )
        if index.id:
            logger.info("Created index: %s", index.id)
            return index.id
        raise RuntimeError("Index creation returned empty ID")
    except Exception as e:
        logger.error("Error creating index: %s", e)
        raise


def create_twelvelabs_processor(
    video_path: str,
    video_id: str | None = None,
    video_url: str | None = None,
    output_dir: str = "output",
) -> tuple["VideoProcessor", str]:
    """
    鍒濆鍖?TwelveLabs 瀹㈡埛绔笌绱㈠紩锛屽苟杩斿洖 VideoProcessor 涓?index_id銆?
    """
    twelvelabs_api_key = os.getenv("TWELVELABS_API_KEY")
    if not twelvelabs_api_key:
        raise ValueError("TWELVELABS_API_KEY not found in .env")
    client = TwelveLabs(api_key=twelvelabs_api_key)

    index_id = os.getenv("INDEX_ID")
    if not index_id:
        index_id = create_index(client, index_name=os.getenv("INDEX_NAME"))
        logger.info("Please add this to your .env: INDEX_ID=%s", index_id)

    processor = VideoProcessor(
        client=client,
        index_id=index_id,
        video_path=video_path,
        video_id=video_id,
        video_url=video_url,
        output_dir=output_dir,
    )
    return processor, index_id


def _compute_retry_wait(headers: dict | None, fallback_seconds: float) -> float:
    if not headers:
        return fallback_seconds

    # Retry-After can be seconds or HTTP-date
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after:
        try:
            # numeric seconds
            val = float(retry_after)
            # if it's epoch-ish, convert to delta
            if val > 1_000_000_000:
                now = time.time()
                return max(0.0, val - now)
            return max(0.0, val)
        except Exception:
            # try HTTP-date
            try:
                dt = datetime.datetime.strptime(retry_after, "%a, %d %b %Y %H:%M:%S %Z")
                now = datetime.datetime.utcnow()
                return max(0.0, (dt - now).total_seconds())
            except Exception:
                pass

    # Prefer specific reset headers if present (epoch seconds)
    reset_candidates = []
    for key in (
        "x-ratelimit-outputtoken-reset",
        "x-ratelimit-request-reset",
        "x-ratelimit-duration-reset",
        "x-ratelimit-reset",
    ):
        v = headers.get(key) or headers.get(key.title())
        if v:
            try:
                reset_candidates.append(float(v))
            except Exception:
                pass
    if reset_candidates:
        now = time.time()
        wait = min(reset_candidates) - now
        return max(0.0, wait)

    return fallback_seconds

class VideoProcessor:
    """
    鍒涘缓瑙嗛鍒嗘瀽浠诲姟锛屾彁鍙栫粨鏋勫寲鍥炵瓟
    """
    def __init__(
        self,
        client,
        index_id: str,
        video_path: str,
        video_id: str | None = None,
        video_url: str | None = None,
        output_dir: str = "output",
    ):
        self.client = client
        self.index_id = index_id
        self.video_path = video_path
        self.video_url = video_url
        self.output_dir = output_dir
        self.task_id: str | None = None
        self.indexed_asset_id: str | None = None
        self._video_ready: bool = False
        self._last_analyze_headers: dict | None = None
        if video_id:
            self.video_id = video_id
            return
        self.video_id = self.create_video_task(video_path)
        
    # def create_video_task(self, video_path: str):
    #     """
    #     涓婁紶瑙嗛鑾峰彇video_id
    #     """
    #     max_retries = int(os.getenv("TWELVELABS_UPLOAD_RETRIES", "3"))
    #     retry_interval = float(os.getenv("TWELVELABS_UPLOAD_RETRY_INTERVAL", "5"))
    #     timeout_seconds = float(os.getenv("TWELVELABS_UPLOAD_TIMEOUT", "600"))

    #     def _supports_param(fn, name: str) -> bool:
    #         try:
    #             return name in inspect.signature(fn).parameters
    #         except Exception:
    #             return False

    #     print("瑙嗛璺緞锛?, video_path)
    #     print("瑙嗛澶у皬锛?, os.path.getsize(video_path) if os.path.exists(video_path) else "unknown")
    #     with open(video_path, "rb") as f:
    #         for attempt in range(1, max_retries + 1):
    #             try:
    #                 create_kwargs = {
    #                     "index_id": self.index_id,
    #                     "video_file": (f.name, f, "video/mp4"),
    #                 }
    #                 if _supports_param(self.client.tasks.create, "timeout"):
    #                     create_kwargs["timeout"] = timeout_seconds
    #                 elif _supports_param(self.client.tasks.create, "request_timeout"):
    #                     create_kwargs["request_timeout"] = timeout_seconds
    #                 print("瑙嗛涓婁紶涓細", timeout_seconds, "绉掑悗灏嗚秴鏃讹紝鍙傛暟锛?, create_kwargs)
    #                 task = self.client.tasks.create(**create_kwargs)
    #                 logger.info("Created task: id=%s, video_id=%s", task.id, task.video_id)
    #                 self.task_id = task.id
    #                 return task.video_id
    #             except Exception:
    #                 logger.exception(
    #                     "Upload failed (attempt %d/%d). video_path=%s size=%s bytes",
    #                     attempt,
    #                     max_retries,
    #                     video_path,
    #                     os.path.getsize(video_path) if os.path.exists(video_path) else "unknown",
    #                 )
    #                 if attempt < max_retries:
    #                     time.sleep(retry_interval)
    #                     continue
    #                 raise

    def _wait_for_video_ready(self) -> None:
        if self._video_ready or not self.task_id:
            return

        max_wait_seconds = int(os.getenv("TWELVELABS_MAX_WAIT_SECONDS", "120"))
        poll_interval = float(os.getenv("TWELVELABS_POLL_INTERVAL", "5"))
        deadline = time.time() + max_wait_seconds

        while time.time() < deadline:
            task = self.client.tasks.retrieve(self.task_id)
            status = (task.status or "").lower()
            logger.info("Video task status: %s (task_id=%s)", status, self.task_id)
            if status == "ready":
                self._video_ready = True
                return
            if status == "failed":
                raise RuntimeError(f"Video indexing failed (task_id={self.task_id}).")
            time.sleep(poll_interval)

        raise TimeoutError(
            f"Timed out waiting for video indexing (task_id={self.task_id}, "
            f"waited {max_wait_seconds}s)."
        )

    def _analyze_with_retry(self, **kwargs):
        max_retries = int(os.getenv("TWELVELABS_ANALYZE_RETRIES", "12"))
        retry_interval = float(os.getenv("TWELVELABS_RETRY_INTERVAL", "5"))

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.client.analyze(**kwargs)
                headers = getattr(resp, "headers", None)
                if isinstance(headers, dict):
                    self._last_analyze_headers = headers
                return resp
            except BadRequestError as e:
                body = getattr(e, "body", None)
                code = body.get("code") if isinstance(body, dict) else None
                if code != "video_not_ready":
                    raise
                logger.warning(
                    "Video not ready yet, retrying... (%d/%d)",
                    attempt,
                    max_retries,
                )
                time.sleep(retry_interval)
            except Exception as e:
                # Rate limit handling (HTTP 429)
                status_code = getattr(e, "status_code", None)
                body = getattr(e, "body", None)
                headers = getattr(e, "headers", None)
                if isinstance(headers, dict):
                    self._last_analyze_headers = headers
                code = None
                message = None
                if isinstance(body, dict):
                    code = body.get("code")
                    message = body.get("message")
                is_rate_limited = (
                    isinstance(e, TooManyRequestsError)
                    or status_code == 429
                    or code == "too_many_requests"
                    or "TooManyRequests" in e.__class__.__name__
                )
                if is_rate_limited:
                    wait_seconds = _compute_retry_wait(headers if isinstance(headers, dict) else None, retry_interval)
                    if message:
                        logger.warning("Rate limit error message: %s", message)
                    logger.warning(
                        "Rate limited (429). Waiting %ss before retry... (%d/%d)",
                        int(wait_seconds),
                        attempt,
                        max_retries,
                    )
                    time.sleep(wait_seconds)
                    continue

                # Timeout handling
                is_timeout = False
                if httpx is not None and isinstance(e, getattr(httpx, "ReadTimeout", ())):
                    is_timeout = True
                if httpcore is not None and isinstance(e, getattr(httpcore, "ReadTimeout", ())):
                    is_timeout = True
                if isinstance(e, TimeoutError):
                    is_timeout = True
                if not is_timeout and "ReadTimeout" not in e.__class__.__name__:
                    raise
                logger.warning(
                    "Analyze request timed out, retrying... (%d/%d)",
                    attempt,
                    max_retries,
                )
                time.sleep(retry_interval)
        raise TimeoutError("Video still not ready after retries. Please try again later.")
    

    def create_video_task(self, video_path: str):
        """
        Uploads and indexes video. If video_path is a public URL, use video_url.
        """
        if self.video_url:
            return self._create_video_via_url(self.video_url)
        if video_path.startswith("http://") or video_path.startswith("https://"):
            return self._create_video_via_url(video_path)
        file_size = os.path.getsize(video_path) if os.path.exists(video_path) else 0
        if file_size > 200 * 1024 * 1024:
            logger.info("Using multipart upload for large file (%s bytes).", file_size)
            return self._create_video_via_multipart(video_path)
        return self._create_video_via_task(video_path)

    def _create_video_via_task(self, video_path: str) -> str:
        max_retries = int(os.getenv("TWELVELABS_UPLOAD_RETRIES", "3"))
        retry_interval = float(os.getenv("TWELVELABS_UPLOAD_RETRY_INTERVAL", "5"))
        timeout_seconds = float(os.getenv("TWELVELABS_UPLOAD_TIMEOUT", "600"))

        def _supports_param(fn, name: str) -> bool:
            try:
                return name in inspect.signature(fn).parameters
            except Exception:
                return False

        with open(video_path, "rb") as f:
            for attempt in range(1, max_retries + 1):
                try:
                    create_kwargs = {
                        "index_id": self.index_id,
                        "video_file": (f.name, f, "video/mp4"),
                    }
                    if _supports_param(self.client.tasks.create, "timeout"):
                        create_kwargs["timeout"] = timeout_seconds
                    elif _supports_param(self.client.tasks.create, "request_timeout"):
                        create_kwargs["request_timeout"] = timeout_seconds

                    task = self.client.tasks.create(**create_kwargs)
                    logger.info("Created task: id=%s, video_id=%s", task.id, task.video_id)
                    self.task_id = task.id
                    return task.video_id
                except Exception:
                    logger.exception(
                        "Upload failed (attempt %d/%d). video_path=%s size=%s bytes",
                        attempt,
                        max_retries,
                        video_path,
                        os.path.getsize(video_path) if os.path.exists(video_path) else "unknown",
                    )
                    if attempt < max_retries:
                        time.sleep(retry_interval)
                        continue
                    raise

    def _create_video_via_url(self, video_url: str) -> str:
        max_retries = int(os.getenv("TWELVELABS_UPLOAD_RETRIES", "3"))
        retry_interval = float(os.getenv("TWELVELABS_UPLOAD_RETRY_INTERVAL", "5"))
        timeout_seconds = float(os.getenv("TWELVELABS_UPLOAD_TIMEOUT", "600"))

        def _supports_param(fn, name: str) -> bool:
            try:
                return name in inspect.signature(fn).parameters
            except Exception:
                return False

        for attempt in range(1, max_retries + 1):
            try:
                create_kwargs = {
                    "index_id": self.index_id,
                    "video_url": video_url,
                }
                if _supports_param(self.client.tasks.create, "timeout"):
                    create_kwargs["timeout"] = timeout_seconds
                elif _supports_param(self.client.tasks.create, "request_timeout"):
                    create_kwargs["request_timeout"] = timeout_seconds

                task = self.client.tasks.create(**create_kwargs)
                logger.info("Created task: id=%s, video_id=%s", task.id, task.video_id)
                self.task_id = task.id
                return task.video_id
            except Exception:
                logger.exception(
                    "Upload by URL failed (attempt %d/%d). video_url=%s",
                    attempt,
                    max_retries,
                    video_url,
                )
                if attempt < max_retries:
                    time.sleep(retry_interval)
                    continue
                raise

    def _get_multipart_wrapper(self):
        candidates = [
            ("assets", "multipart_uploads"),
            ("assets", "multipart_upload"),
            ("multipart_uploads",),
            ("multipart_upload",),
            ("uploads", "multipart_uploads"),
            ("uploads", "multipart_upload"),
        ]
        for path in candidates:
            obj = self.client
            ok = True
            for attr in path:
                if not hasattr(obj, attr):
                    ok = False
                    break
                obj = getattr(obj, attr)
            if ok and hasattr(obj, "upload_file"):
                return obj
        raise RuntimeError(
            "Multipart upload wrapper not found on TwelveLabs client. "
            "Please ensure the twelvelabs SDK is up to date."
        )

    def _create_video_via_multipart(self, video_path: str) -> str:
        upload_batch_size = int(os.getenv("TWELVELABS_UPLOAD_BATCH_SIZE", "10"))
        upload_workers = int(os.getenv("TWELVELABS_UPLOAD_WORKERS", "5"))
        upload_retries = int(os.getenv("TWELVELABS_UPLOAD_RETRIES", "3"))
        upload_retry_delay = float(os.getenv("TWELVELABS_UPLOAD_RETRY_INTERVAL", "1"))

        wrapper = self._get_multipart_wrapper()

        def _progress_cb(progress):
            try:
                logger.info(
                    "Upload progress: %s/%s (%.1f%%) status=%s",
                    progress.completed_chunks,
                    progress.total_chunks,
                    float(progress.percentage),
                    progress.status,
                )
            except Exception:
                pass

        upload_result = wrapper.upload_file(
            video_path,
            batch_size=upload_batch_size,
            max_workers=upload_workers,
            progress_callback=_progress_cb,
            max_retries=upload_retries,
            retry_delay=upload_retry_delay,
        )
        asset_id = getattr(upload_result, "asset_id", None)
        if not asset_id:
            raise RuntimeError("Multipart upload did not return asset_id.")

        indexed = self.client.indexes.indexed_assets.create(
            self.index_id,
            asset_id=asset_id,
        )
        indexed_id = getattr(indexed, "id", None)
        if not indexed_id:
            raise RuntimeError("Indexing did not return indexed asset id.")

        self.task_id = None
        self.indexed_asset_id = indexed_id
        return indexed_id

    def _wait_for_video_ready(self) -> None:
        if self._video_ready:
            return
        if self.indexed_asset_id:
            return self._wait_for_indexed_asset_ready()
        if not self.task_id:
            return

        max_wait_seconds = int(os.getenv("TWELVELABS_MAX_WAIT_SECONDS", "600"))
        poll_interval = float(os.getenv("TWELVELABS_POLL_INTERVAL", "5"))
        deadline = time.time() + max_wait_seconds

        while time.time() < deadline:
            task = self.client.tasks.retrieve(self.task_id)
            status = (task.status or "").lower()
            logger.info("Video task status: %s (task_id=%s)", status, self.task_id)
            if status == "ready":
                self._video_ready = True
                return
            if status == "failed":
                raise RuntimeError(f"Video indexing failed (task_id={self.task_id}).")
            time.sleep(poll_interval)

        raise TimeoutError(
            f"Timed out waiting for video indexing (task_id={self.task_id}, "
            f"waited {max_wait_seconds}s)."
        )

    def _wait_for_indexed_asset_ready(self) -> None:
        max_wait_seconds = int(os.getenv("TWELVELABS_MAX_WAIT_SECONDS", "600"))
        poll_interval = float(os.getenv("TWELVELABS_POLL_INTERVAL", "5"))
        deadline = time.time() + max_wait_seconds

        while time.time() < deadline:
            asset = self.client.indexes.indexed_assets.retrieve(
                self.index_id,
                self.indexed_asset_id,
            )
            status = (getattr(asset, "status", None) or "").lower()
            logger.info(
                "Indexed asset status: %s (indexed_asset_id=%s)",
                status,
                self.indexed_asset_id,
            )
            if status == "ready":
                self._video_ready = True
                return
            if status == "failed":
                raise RuntimeError(
                    f"Indexing failed (indexed_asset_id={self.indexed_asset_id})."
                )
            time.sleep(poll_interval)

        raise TimeoutError(
            f"Timed out waiting for indexing (indexed_asset_id={self.indexed_asset_id}, "
            f"waited {max_wait_seconds}s)."
        )

    def get_today_analyze_token_remaining(self) -> dict | None:
        headers = self._last_analyze_headers
        if not headers:
            return None
        remaining = headers.get("X-RateLimit-OutputToken-Remaining") or headers.get("x-ratelimit-outputtoken-remaining")
        limit = headers.get("X-RateLimit-OutputToken-Limit") or headers.get("x-ratelimit-outputtoken-limit")
        reset = headers.get("X-RateLimit-OutputToken-Reset") or headers.get("x-ratelimit-outputtoken-reset")
        if remaining is None and limit is None and reset is None:
            return None
        try:
            remaining_val = int(remaining) if remaining is not None else None
        except Exception:
            remaining_val = None
        try:
            limit_val = int(limit) if limit is not None else None
        except Exception:
            limit_val = None
        try:
            reset_val = int(float(reset)) if reset is not None else None
        except Exception:
            reset_val = None
        return {"remaining": remaining_val, "limit": limit_val, "reset_epoch": reset_val}

    def analyze_events(self, ctx: ActivityContext) -> Dict[str, Any]:
        """浜嬩欢鍒嗗壊"""
        token_info = self.get_today_analyze_token_remaining()
        if token_info:
            self._wait_for_video_ready()
        with open("prompts/event_split_prompt.md", "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(
                activity=ctx.activity,
                people=ctx.people,
                time=ctx.time,
                location=ctx.location,
                )
        # print("------Event split prompt------\n")
        # print(prompt)

        if self.video_id:
            event_resp = self._analyze_with_retry(
                video_id=self.video_id,
                prompt=prompt,
                temperature=0.2,
                response_format=ResponseFormat(
                        json_schema={
                            "activity_description": "string",
                            "activity_visual_clue": {
                                "frame": "HH:MM:SS(.sss)",
                                "description": "string",
                            },
                            "event_count": "integer",
                            "events": [
                                {
                                    "id": "string",
                                    "name": "string",
                                    "description": "string",
                                    "start_time": "HH:MM:SS(.sss)",
                                    "end_time": "HH:MM:SS(.sss)",
                                    "scene_clues": [
                                        {"frame": "HH:MM:SS(.sss)", "description": "string"}
                                    ],
                                }
                            ],
                        },
                ),
            )
            event_finish = getattr(event_resp, "finish_reason", None)
            if event_finish:
                logger.info("Event analyze finish_reason: %s", event_finish)

            token_info = self.get_today_analyze_token_remaining()
            if token_info:
                logger.info("Analyze token remaining (post): %s", token_info)

            if not event_resp.data:
                return {}
            
            # 淇濆瓨鍘熷response
            raw = event_resp.data
            logger.debug("raw type: %s", type(raw))
            try:
                logger.debug("raw len: %s", len(raw))
            except Exception:
                logger.debug("raw len: %s", len(str(raw)))

            out_dir = os.path.join(self.output_dir, "twelvelabs")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, "raw_event_resp.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(raw if isinstance(raw, str) else str(raw))

            # 瑙ｆ瀽response
            max_parse_retries = int(os.getenv("TWELVELABS_PARSE_RETRIES", "2"))
            last_err = None
            for attempt in range(1, max_parse_retries + 1):
                try:
                    if _looks_truncated_or_garbage(raw):
                        try:
                            if os.path.exists(out_path):
                                os.remove(out_path)
                        except Exception:
                            pass
                        logger.warning(
                            "Raw event response looks truncated/garbled, retrying analyze... (%d/%d)",
                            attempt,
                            max_parse_retries,
                        )
                        time.sleep(float(os.getenv("TWELVELABS_PARSE_RETRY_DELAY", "3")))
                        event_resp = self._analyze_with_retry(
                            video_id=self.video_id,
                            prompt=prompt,
                            temperature=0.2,
                            response_format=ResponseFormat(
                                    json_schema={
                                        "activity_description": "string",
                                        "activity_visual_clue": {
                                            "frame": "HH:MM:SS(.sss)",
                                            "description": "string",
                                        },
                                        "event_count": "integer",
                                        "events": [
                                            {
                                                "id": "string",
                                                "name": "string",
                                                "description": "string",
                                                "start_time": "HH:MM:SS(.sss)",
                                                "end_time": "HH:MM:SS(.sss)",
                                                "scene_clues": [
                                                    {"frame": "HH:MM:SS(.sss)", "description": "string"}
                                                ],
                                            }
                                        ],
                                    },
                            ),
                        )
                        raw = event_resp.data
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(raw if isinstance(raw, str) else str(raw))
                        continue
                    event_parsed = parse_json_from_llm(raw)
                    break
                except Exception as e:
                    last_err = e
                    if attempt >= max_parse_retries:
                        raise
                    # 鍒犻櫎涓嶅畬鏁寸殑鍘熷鍝嶅簲锛岄伩鍏嶈鐢?
                    try:
                        if os.path.exists(out_path):
                            os.remove(out_path)
                    except Exception:
                        pass
                    logger.warning("Failed to parse event JSON, retrying analyze... (%d/%d)", attempt, max_parse_retries)
                    time.sleep(float(os.getenv("TWELVELABS_PARSE_RETRY_DELAY", "3")))
                    event_resp = self._analyze_with_retry(
                        video_id=self.video_id,
                        prompt=prompt,
                        temperature=0.2,
                        response_format=ResponseFormat(
                                json_schema={
                                    "activity_description": "string",
                                    "activity_visual_clue": {
                                        "frame": "HH:MM:SS(.sss)",
                                        "description": "string",
                                    },
                                    "event_count": "integer",
                                    "events": [
                                        {
                                            "id": "string",
                                            "name": "string",
                                            "description": "string",
                                            "start_time": "HH:MM:SS(.sss)",
                                            "end_time": "HH:MM:SS(.sss)",
                                            "scene_clues": [
                                                {"frame": "HH:MM:SS(.sss)", "description": "string"}
                                            ],
                                        }
                                    ],
                                },
                        ),
                    )
                    raw = event_resp.data
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(raw if isinstance(raw, str) else str(raw))
            logger.info("----- Event split response (parsed JSON) ------")
            logger.info(json.dumps(event_parsed, ensure_ascii=False, indent=2))
            return event_parsed

        else:
            raise ValueError("Invalid VIDEO_ID.")
    

    def analyze_entities(
        self,
        ctx: EntityContext,
        event_id: str,
        event_description: str,
        start_ts: str,
        end_ts: str,
    ) -> Dict[str, Any]:
        """涓哄瓙浜嬩欢鎻愬彇瀹炰綋椤圭洰"""
        token_info = self.get_today_analyze_token_remaining()
        if token_info:
            self._wait_for_video_ready()
        with open("prompts/entity_extract_prompt.md", "r", encoding="utf-8") as f:
            template = Template(f.read())
            entity_prompt = template.render(
                activity=ctx.activity,
                people=ctx.people,
                time=ctx.time,
                sub_event=ctx.sub_event.strip("銆?) if ctx.sub_event else ctx.sub_event,
                event_time_range=f"{ctx.start_time} - {ctx.end_time}",
            )
        # print("------Entity Extraction Prompt------\n")
        # print(entity_prompt)

        if self.video_id:
            entity_resp = self._analyze_with_retry(
                video_id=self.video_id,
                prompt=entity_prompt,
                response_format=ResponseFormat(
                    json_schema={
                        "key_objects": [
                            {
                                "id": "string",
                                "item_name": "string",
                                "key_frame": "HH:MM:SS(.sss)",
                                "coordinates": {
                                    "x": "integer",
                                    "y": "integer",
                                    "width": "integer",
                                    "height": "integer",
                                },
                                "details": {
                                    "visual": ["string"],
                                    "semantic": ["string"],
                                },
                                "detail_pairs": {
                                    "category": "string",
                                    "detail": "string",
                                },
                            }
                        ],
                    }
                ),
            )
            entity_finish = getattr(entity_resp, "finish_reason", None)
            if entity_finish:
                logger.info("Entity analyze finish_reason: %s", entity_finish)

            token_info = self.get_today_analyze_token_remaining()
            if token_info:
                logger.info("Analyze token remaining (post): %s", token_info)

            logger.info("------ Entity extract response ------")
            logger.info("Event %s, start: %s, end: %s", event_description, start_ts, end_ts)
            if not entity_resp.data:
                logger.warning("No data returned from LLM.")
                return {}
            
            raw = entity_resp.data
            out_dir = os.path.join(self.output_dir, "twelvelabs")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"raw_entity_resp_event_{event_id}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(raw if isinstance(raw, str) else str(raw))

            # 瑙ｆ瀽response
            def _timestamp_to_seconds(ts: str) -> float:
                parts = ts.split(":")
                if len(parts) == 3:
                    h, m, s = map(float, parts)
                    return h * 3600 + m * 60 + s
                if len(parts) == 2:
                    m, s = map(float, parts)
                    return m * 60 + s
                if len(parts) == 1:
                    return float(parts[0])
                raise ValueError(f"Unsupported timestamp format: {ts}")

            max_parse_retries = int(os.getenv("TWELVELABS_PARSE_RETRIES", "2"))
            last_err = None
            for attempt in range(1, max_parse_retries + 1):
                try:
                    if _looks_truncated_or_garbage(raw):
                        try:
                            if os.path.exists(out_path):
                                os.remove(out_path)
                        except Exception:
                            pass
                        logger.warning(
                            "Raw entity response looks truncated/garbled, retrying analyze... (%d/%d)",
                            attempt,
                            max_parse_retries,
                        )
                        time.sleep(float(os.getenv("TWELVELABS_PARSE_RETRY_DELAY", "3")))
                        entity_resp = self._analyze_with_retry(
                            video_id=self.video_id,
                            prompt=entity_prompt,
                            response_format=ResponseFormat(
                                json_schema={
                                    "key_objects": [
                                        {
                                            "id": "string",
                                            "item_name": "string",
                                            "key_frame": "HH:MM:SS(.sss)",
                                            "coordinates": {
                                                "x": "integer",
                                                "y": "integer",
                                                "width": "integer",
                                                "height": "integer",
                                            },
                                            "details": {
                                                "visual": ["string"],
                                                "semantic": ["string"],
                                            },
                                            "detail_pairs": {
                                    "category": "string",
                                    "detail": "string",
                                },
                                        }
                                    ],
                                }
                            ),
                        )
                        entity_finish = getattr(entity_resp, "finish_reason", None)
                        if entity_finish:
                            logger.info("Entity analyze finish_reason: %s", entity_finish)
                        raw = entity_resp.data
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(raw if isinstance(raw, str) else str(raw))
                        continue
                    entity_parsed = parse_json_from_llm(raw)
                    break
                except Exception as e:
                    last_err = e
                    if attempt >= max_parse_retries:
                        raise
                    # 鍒犻櫎涓嶅畬鏁寸殑鍘熷鍝嶅簲锛岄伩鍏嶈鐢?
                    try:
                        if os.path.exists(out_path):
                            os.remove(out_path)
                    except Exception:
                        pass
                    logger.warning("Failed to parse entity JSON, retrying analyze... (%d/%d)", attempt, max_parse_retries)
                    time.sleep(float(os.getenv("TWELVELABS_PARSE_RETRY_DELAY", "3")))
                    entity_resp = self._analyze_with_retry(
                        video_id=self.video_id,
                        prompt=entity_prompt,
                        response_format=ResponseFormat(
                            json_schema={
                                "key_objects": [
                                    {
                                        "id": "string",
                                        "item_name": "string",
                                        "key_frame": "HH:MM:SS(.sss)",
                                        "coordinates": {
                                            "x": "integer",
                                            "y": "integer",
                                            "width": "integer",
                                            "height": "integer",
                                        },
                                        "details": {
                                            "visual": ["string"],
                                            "semantic": ["string"],
                                        },
                                        "detail_pairs": {
                                            "category": "string",
                                            "detail": "string",
                                        },
                                    }
                                ],
                            }
                        ),
                    )
                    entity_finish = getattr(entity_resp, "finish_reason", None)
                    if entity_finish:
                        logger.info("Entity analyze finish_reason: %s", entity_finish)
                    raw = entity_resp.data
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(raw if isinstance(raw, str) else str(raw))
            try:
                start_sec = _timestamp_to_seconds(start_ts)
                end_sec = _timestamp_to_seconds(end_ts)
            except Exception as e:
                logger.warning(f"Failed to parse event time range ({start_ts}-{end_ts}): {e}")
                start_sec, end_sec = None, None

            if isinstance(entity_parsed, dict) and start_sec is not None and end_sec is not None:
                items = entity_parsed.get("key_objects", [])
                if isinstance(items, list):
                    filtered = []
                    for item in items:
                        key_frame = item.get("key_frame")
                        if not key_frame:
                            continue
                        try:
                            ts_sec = _timestamp_to_seconds(key_frame)
                        except Exception:
                            continue
                        if start_sec <= ts_sec <= end_sec:
                            filtered.append(item)
                        else:
                            logger.warning(
                                f"Drop entity outside event range: event={event_id}, key_frame={key_frame}"
                            )
                    entity_parsed["key_objects"] = filtered
            logger.info(json.dumps(entity_parsed, ensure_ascii=False, indent=2))
            return entity_parsed
        
        else:
            raise ValueError("Invalid VIDEO_ID.")

