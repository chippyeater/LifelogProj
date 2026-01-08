from typing import Optional
from dataclasses import dataclass
import json

@dataclass
class ActivityContext:
    activity: str
    people: str
    time: str
    location: str
    sub_event: Optional[str]


@dataclass
class EntityContext:
    activity: str
    people: str
    time: str
    start_time: str
    end_time: str
    sub_event: Optional[str]


def parse_json_from_llm(data_str: str) -> dict:
    if data_str is None:
        raise ValueError("LLM returned None")

    s = data_str.strip()
    if s.startswith("```json"):
        s = s[7:].strip()
    if s.startswith("```"):
        s = s[3:].strip()
    if s.endswith("```"):
        s = s[:-3].strip()

    return json.loads(s)


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
    