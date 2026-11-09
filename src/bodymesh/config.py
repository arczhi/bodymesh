"""Configuration for a Bodymesh node.

Everything that could differ between machines lives here and is resolved from
the environment at call time (never import time), so tests can monkeypatch it
and the same wheel can run on a laptop or a VPS.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP = "bodymesh"
DEFAULT_TZ = "Asia/Shanghai"


def _path(env_key: str, default: str) -> Path:
    raw = os.environ.get(env_key) or default
    return Path(raw).expanduser()


def home() -> Path:
    """Directory that holds the database, inbox and cached tokens."""
    p = _path("BODYMESH_HOME", "~/.bodymesh")
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    p = _path("BODYMESH_DB", str(home() / "bodymesh.db"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def inbox_dir() -> Path:
    """Where chat attachments (meal photos) land before they are ingested."""
    p = _path("BODYMESH_INBOX", str(home() / "inbox"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def garmin_token_dir() -> Path:
    return _path("GARMINTOKENS", "~/.garminconnect")


def garmin_is_cn() -> bool:
    return (os.environ.get("GARMIN_IS_CN", "true") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def llm_key() -> str | None:
    """API key for the estimation model (also used for food-photo analysis)."""
    return (
        os.environ.get("BODYMESH_LLM_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def llm_base() -> str:
    return (os.environ.get("BODYMESH_LLM_BASE") or "https://api.deepseek.com").rstrip("/")


def chat_model() -> str:
    return os.environ.get("BODYMESH_CHAT_MODEL") or "deepseek-v4-pro"


def vision_model() -> str:
    return os.environ.get("BODYMESH_VISION_MODEL") or "deepseek-flash"


def tz_name() -> str:
    return os.environ.get("BODYMESH_TZ") or DEFAULT_TZ


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(tz_name())
    except ZoneInfoNotFoundError:  # pragma: no cover - depends on host tzdata
        return ZoneInfo("UTC")


def body_weight_kg() -> float | None:
    """Optional fallback body weight for per-kg metrics before any weigh-in."""
    raw = os.environ.get("BODYMESH_WEIGHT_KG")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def sleep_need_hours() -> float:
    try:
        return float(os.environ.get("BODYMESH_SLEEP_NEED_H", "8"))
    except ValueError:
        return 8.0


def goal_weight_kg() -> float | None:
    raw = os.environ.get("BODYMESH_GOAL_WEIGHT_KG")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
