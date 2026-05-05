import datetime
import os
import sqlite3
import json
from contextlib import closing
from typing import Any, Dict, Optional


_PIPELINE_DEFAULT = {
    "events": "pending",
    "entities": "pending",
    "frames": "pending",
    "aigc": "pending",
    "unity": "pending",
    "last_error": None,
}

def get_pipeline_state(db_path: str, user_id: str, video_name: str) -> Dict[str, Any]:
    rec = get_video_record(db_path, user_id, video_name)
    if not rec:
        return dict(_PIPELINE_DEFAULT)
    raw = rec.get("pipeline_state")
    if not raw:
        return dict(_PIPELINE_DEFAULT)
    try:
        state = json.loads(raw)
        if not isinstance(state, dict):
            return dict(_PIPELINE_DEFAULT)
        # 补齐缺失键（兼容旧版本）
        for k, v in _PIPELINE_DEFAULT.items():
            state.setdefault(k, v)
        return state
    except Exception:
        return dict(_PIPELINE_DEFAULT)

def set_pipeline_state(db_path: str, user_id: str, video_name: str, state: Dict[str, Any]) -> None:
    # 兜底补齐键
    s = dict(_PIPELINE_DEFAULT)
    s.update(state or {})
    upsert_user_video(
        db_path,
        user_id=user_id,
        video_name=video_name,
        fields={"pipeline_state": json.dumps(s, ensure_ascii=False)},
    )

def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _connect(db_path: str) -> sqlite3.Connection:
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(db_path)


def _normalize_video_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return path
    if os.path.isabs(path):
        base = os.getcwd()
        try:
            path = os.path.relpath(path, base)
        except Exception:
            path = os.path.basename(path)
    return os.path.normcase(os.path.normpath(path))


def init_db(db_path: str) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_pipeline (
                user_id TEXT PRIMARY KEY,
                video_path TEXT,
                index_id TEXT,
                video_id TEXT,
                status TEXT,
                extracted_context_path TEXT,
                gameflow_path TEXT,
                gamemeta_path TEXT,
                subevent_count INTEGER,
                processed_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        # Create new schema with composite PK
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_videos (
                user_id TEXT,
                video_name TEXT,
                video_path TEXT,
                video_id TEXT,
                video_url TEXT,
                video_object_key TEXT,
                index_id TEXT,
                status TEXT,
                extracted_context_path TEXT,
                gameflow_path TEXT,
                gamemeta_path TEXT,
                subevent_count INTEGER,
                processed_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (user_id, video_name)
            )
            """
        )

        # Best-effort migration from legacy schema if needed
        try:
            cur = conn.execute("PRAGMA table_info(user_videos)")
            cols = [r[1] for r in cur.fetchall()]
            if "video_name" not in cols:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_videos_new (
                        user_id TEXT,
                        video_name TEXT,
                        video_path TEXT,
                        video_id TEXT,
                        video_url TEXT,
                        video_object_key TEXT,
                        index_id TEXT,
                        status TEXT,
                        extracted_context_path TEXT,
                        gameflow_path TEXT,
                        gamemeta_path TEXT,
                        subevent_count INTEGER,
                        processed_at TEXT,
                        created_at TEXT,
                        updated_at TEXT,
                        PRIMARY KEY (user_id, video_name)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_videos_new (
                        user_id, video_name, video_path, video_id, video_url, video_object_key,
                        index_id, status, extracted_context_path, gameflow_path, gamemeta_path,
                        subevent_count, processed_at, created_at, updated_at
                    )
                    SELECT
                        user_id,
                        COALESCE(
                            NULLIF(TRIM(substr(video_path, instr(video_path, '/') + 1)), ''),
                            NULLIF(TRIM(substr(video_path, instr(video_path, '\\\\') + 1)), ''),
                            video_id
                        ) AS video_name,
                        video_path,
                        video_id,
                        video_url,
                        video_object_key,
                        index_id,
                        status,
                        extracted_context_path,
                        gameflow_path,
                        gamemeta_path,
                        subevent_count,
                        processed_at,
                        created_at,
                        updated_at
                    FROM user_videos
                    """
                )
                conn.execute("DROP TABLE user_videos")
                conn.execute("ALTER TABLE user_videos_new RENAME TO user_videos")
        except Exception:
            pass
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_videos_video_path
            ON user_videos(video_path)
            """
        )
        try:
            cur = conn.execute("PRAGMA table_info(user_videos)")
            cols = [r[1] for r in cur.fetchall()]
            if "pipeline_state" not in cols:
                conn.execute("ALTER TABLE user_videos ADD COLUMN pipeline_state TEXT")
        except Exception:
            pass
        conn.commit()


def upsert_user_video(
    db_path: str,
    user_id: Optional[str],
    video_name: Optional[str],
    fields: Dict[str, Any],
) -> None:
    if not user_id or not video_name:
        return
    if "video_path" in fields:
        fields["video_path"] = _normalize_video_path(fields.get("video_path"))
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM user_videos WHERE user_id = ? AND video_name = ? LIMIT 1",
            (user_id, video_name),
        )
        exists = cur.fetchone() is not None

        if exists:
            updates = []
            params = []
            for k, v in fields.items():
                if v is None:
                    continue
                updates.append(f"{k} = ?")
                params.append(v)
            updates.append("updated_at = ?")
            params.append(now)
            params.append(user_id)
            params.append(video_name)
            if updates:
                conn.execute(
                    f"UPDATE user_videos SET {', '.join(updates)} WHERE user_id = ? AND video_name = ?",
                    params,
                )
        else:
            insert_fields = dict(fields)
            insert_fields["created_at"] = now
            insert_fields["updated_at"] = now
            cols = ["user_id", "video_name"] + list(insert_fields.keys())
            vals = [user_id, video_name] + list(insert_fields.values())
            placeholders = ", ".join(["?"] * len(vals))
            conn.execute(
                f"INSERT INTO user_videos ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
        conn.commit()


def get_video_record(db_path: str, user_id: str, video_name: str) -> Optional[dict]:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """
            SELECT *
            FROM user_videos
            WHERE user_id = ? AND video_name = ?
            LIMIT 1
            """,
            (user_id, video_name),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def get_latest_video_record(db_path: str, user_id: str) -> Optional[dict]:
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """
            SELECT *
            FROM user_videos
            WHERE user_id = ?
            ORDER BY
                CASE
                    WHEN updated_at IS NULL OR TRIM(updated_at) = '' THEN 1
                    ELSE 0
                END,
                updated_at DESC,
                CASE
                    WHEN processed_at IS NULL OR TRIM(processed_at) = '' THEN 1
                    ELSE 0
                END,
                processed_at DESC,
                created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def count_subevents(extracted_context_path: str) -> Optional[int]:
    try:
        import json

        with open(extracted_context_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        events = data.get("events")
        if isinstance(events, list):
            return len(events)
    except Exception:
        return None
    return None
