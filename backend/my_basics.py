import re
from typing import Optional
from dataclasses import dataclass
import json

@dataclass
class ActivityContext:
    activity: str
    people: str
    time: str
    location: str
    # sub_event: Optional[str]
    video_length: Optional[str] = None


@dataclass
class EntityContext:
    activity: str
    people: str
    time: str
    start_time: str
    end_time: str
    sub_event: Optional[str]


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:].strip()
    if s.startswith("```"):
        s = s[3:].strip()
    if s.endswith("```"):
        s = s[:-3].strip()
    return s

def _extract_first_json_object(s: str) -> str:
    """
    从一段可能夹杂解释文本的字符串中，提取第一个 {...} 或 [...] 的完整 JSON 段。
    """
    s = s.strip()
    # 优先找对象
    start_candidates = [(s.find("{"), "{", "}"), (s.find("["), "[", "]")]
    start_candidates = [(i, o, c) for (i, o, c) in start_candidates if i != -1]
    if not start_candidates:
        return s

    start, open_ch, close_ch = min(start_candidates, key=lambda x: x[0])

    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(s)):
        ch = s[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return s[start:idx+1]
    return s[start:]  # 找不到闭合就尽量返回尾部

def _extract_balanced_json(s: str) -> str:
    """
    ?????? JSON ??/???????????
    ??????????????????
    """
    s = s.strip()
    start_candidates = [(s.find("{"), "{", "}"), (s.find("["), "[", "]")]
    start_candidates = [(i, o, c) for (i, o, c) in start_candidates if i != -1]
    if not start_candidates:
        return s

    start, open_ch, close_ch = min(start_candidates, key=lambda x: x[0])

    depth = 0
    in_str = False
    esc = False
    last_complete = -1
    for idx in range(start, len(s)):
        ch = s[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    last_complete = idx
    if last_complete != -1:
        return s[start:last_complete + 1]
    return s[start:]

def _escape_invalid_backslashes(s: str) -> str:
    """
    把单个反斜杠转义为字面反斜杠。
    """
    
    # 先处理 \u 后面不是4位hex的情况：把 \u 变成 \\u
    s = re.sub(r'\\u(?![0-9a-fA-F]{4})', r'\\\\u', s)

    # 处理其他非法转义：\ 后面不是合法转义字符时，加一个反斜杠
    # 合法: " \/ b f n r t u
    s = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)
    return s

def parse_json_from_llm(data_str: str) -> dict:
    if data_str is None:
        raise ValueError("LLM returned None")

    s = _strip_code_fences(data_str)
    s = _extract_first_json_object(s)

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        s2 = _escape_invalid_backslashes(s)
        try:
            return json.loads(s2)
        except json.JSONDecodeError:
            s3 = _extract_balanced_json(s2)
            return json.loads(s3)


def ts_to_seconds(ts: str) -> float:
    ts = ts.strip()
    parts = ts.split(":")
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + float(ss)
    elif len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    else:
        raise ValueError(f"Unsupported timestamp format: {ts}")
    
