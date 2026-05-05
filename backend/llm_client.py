import os
import os
import logging
import inspect
import datetime
from typing import Any, Dict

from openai import OpenAI


class SiliconFlowLLM:
    def __init__(self, model: str | None = None) -> None:
        api_key = os.getenv("SILICONFLOW_API_KEY")
        if not api_key:
            raise ValueError("SILICONFLOW_API_KEY not set; cannot call SiliconFlow.")
        self.client = OpenAI(base_url="https://api.siliconflow.cn/v1", api_key=api_key)
        self.model = model or os.getenv("SILICONFLOW_MODEL", "tencent/Hunyuan-MT-7B")
        self.logger = logging.getLogger(__name__)

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 4000,
        model: str | None = None,
        purpose: str | None = None,
    ) -> str:
        use_model = model or self.model
        self.logger.info(
            "SiliconFlow chat call: model=%s temperature=%s max_tokens=%s",
            use_model,
            temperature,
            max_tokens,
        )
        resp = self.client.chat.completions.create(
            model=use_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = getattr(resp, "usage", None)
        if usage:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
            total_tokens = getattr(usage, "total_tokens", None)
            self.logger.info(
                "SiliconFlow token usage: prompt=%s completion=%s total=%s",
                prompt_tokens,
                completion_tokens,
                total_tokens,
            )
        content = resp.choices[0].message.content or ""
        if content:
            self.logger.info("SiliconFlow response: %s", content)
        self._log_response(
            content=content,
            model=use_model,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            total_tokens=getattr(usage, "total_tokens", None) if usage else None,
            purpose=purpose,
        )
        return content

    def translate_text(self, text: str) -> str:
        system_prompt = "翻译为简体中文，只返回译文。"
        return self.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=1000,
            purpose="translate_text",
        )



    def _log_response(
        self,
        *,
        content: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        purpose: str | None,
    ) -> None:
        caller_file, caller_func = self._find_caller()
        log_path = os.getenv(
            "SILICONFLOW_LOG_PATH",
            os.path.join(os.getcwd(), "output", "siliconflow_responses.txt"),
        )
        if not os.path.isabs(log_path):
            log_path = os.path.join(os.getcwd(), log_path)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("----\n")
            f.write(f"time: {timestamp}\n")
            f.write(f"caller: {caller_file}:{caller_func}\n")
            if purpose:
                f.write(f"purpose: {purpose}\n")
            f.write(f"model: {model}\n")
            f.write(f"tokens: prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}\n")
            f.write("response:\n")
            f.write(content)
            f.write("\n")

    def _find_caller(self) -> tuple[str, str]:
        try:
            for frame_info in inspect.stack():
                filename = frame_info.filename
                if filename.endswith("llm_client.py"):
                    continue
                return os.path.basename(filename), frame_info.function
        except Exception:
            pass
        return "unknown", "unknown"

def _looks_like_path(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    if "://" in lower:
        return True
    if "/" in text or "\\" in text:
        return True
    for ext in (".jpg", ".jpeg", ".png", ".mp4", ".mov", ".wav", ".mp3", ".json"):
        if lower.endswith(ext):
            return True
    return False


def _collect_strings(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, out)
    elif isinstance(obj, str):
        if obj.strip():
            out.append(obj)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
        or 0xF900 <= code <= 0xFAFF
    )


