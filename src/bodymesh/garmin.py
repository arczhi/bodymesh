"""Garmin Connect ingestion.

Talks to Garmin through the same ``garminconnect`` library the community MCP
servers wrap, using OAuth tokens from ``~/.garminconnect`` (so no password ever
lands on the server). Every source is fetched defensively: one dead endpoint
must not cost you the whole night's data.
"""

from __future__ import annotations

import json
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Callable

from . import config, db

WARN: list[str] = []


def _warn(msg: str) -> None:
    WARN.append(msg)


def _safe(name: str, fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - third-party lib raises broadly
        _warn(f"{name}: {type(exc).__name__}: {exc}")
        return default


def client() -> Any:
    """Log in with cached OAuth tokens (never prompts)."""
    from garminconnect import Garmin

    g = Garmin(is_cn=config.garmin_is_cn())
    g.login(str(config.garmin_token_dir()))
    return g


# --------------------------------------------------------------------------
# field extraction
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(round(v)) if v is not None else None


def _ts_local(ms: Any) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, config.tz()).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def daily_row(day: str, g: Any) -> dict[str, Any]:
    """Collect one calendar day of wellness data into a ``daily`` row."""
    row: dict[str, Any] = {"date": day}

    stats = _safe(f"stats {day}", lambda: g.get_stats(day), {}) or {}
    row.update(
        {
            "rhr": _num(stats.get("restingHeartRate")),
            "stress_avg": _num(stats.get("averageStressLevel")),
            "stress_max": _num(stats.get("maxStressLevel")),
            "bb_high": _int(stats.get("bodyBatteryHighestValue")),
            "bb_low": _int(stats.get("bodyBatteryLowestValue")),
            "bb_charged": _int(stats.get("bodyBatteryChargedValue")),
            "bb_drained": _int(stats.get("bodyBatteryDrainedValue")),
            "steps": _int(stats.get("totalSteps")),
            "total_kcal": _num(stats.get("totalKilocalories")),
            "active_kcal": _num(stats.get("activeKilocalories")),
            "bmr_kcal": _num(stats.get("bmrKilocalories")),
            "active_s": _int(stats.get("activeSeconds")),
            "highly_active_s": _int(stats.get("highlyActiveSeconds")),
        }
    )
    # Garmin already folds the vigorous 2x weighting into these two counters,
    # so the total is a plain sum (matches what Connect displays).
    moderate = _num(stats.get("moderateIntensityMinutes")) or 0
    vigorous = _num(stats.get("vigorousIntensityMinutes")) or 0
    row["intensity_min"] = _int(moderate + vigorous) if (moderate or vigorous) else None

    sleep = _safe(f"sleep {day}", lambda: g.get_sleep_data(day), {}) or {}
    dto = sleep.get("dailySleepDTO") or {}
    if dto.get("sleepTimeSeconds"):
        scores = dto.get("sleepScores") or {}
        row.update(
            {
                "sleep_s": _int(dto.get("sleepTimeSeconds")),
                "sleep_score": _int((scores.get("overall") or {}).get("value")),
                "sleep_qualifier": (scores.get("overall") or {}).get("qualifierKey"),
                "deep_s": _int(dto.get("deepSleepSeconds")),
                "rem_s": _int(dto.get("remSleepSeconds")),
                "awake_s": _int(dto.get("awakeSleepSeconds")),
                "sleep_stress": _num(dto.get("avgSleepStress")),
                "sleep_hr": _num(dto.get("avgHeartRate")),
                "resp_sleep": _num(dto.get("averageRespirationValue")),
                "sleep_feedback": dto.get("sleepScoreFeedback"),
            }
        )

    hrv = _safe(f"hrv {day}", lambda: g.get_hrv_data(day), {}) or {}
    summary = hrv.get("hrvSummary") or {}
    if summary:
        baseline = summary.get("baseline") or {}
        row.update(
            {
                "hrv_ms": _num(summary.get("lastNightAvg")),
                "hrv_status": summary.get("status"),
                "hrv_base_low": _num(baseline.get("balancedLow")),
                "hrv_base_high": _num(baseline.get("balancedUpper")),
            }
        )

    resp = _safe(f"respiration {day}", lambda: g.get_respiration_data(day), {}) or {}
    if resp:
        row["resp_waking"] = _num(resp.get("avgWakingRespirationValue"))
        if row.get("resp_sleep") is None:
            row["resp_sleep"] = _num(resp.get("avgSleepRespirationValue"))

    status = _safe(f"training_status {day}", lambda: g.get_training_status(day), {}) or {}
    payload = status.get("mostRecentTrainingStatus") or {}
    per_device = payload.get("latestTrainingStatusData") or {}
    inner = next((v for v in per_device.values() if v), None) or {}
    acute = inner.get("acuteTrainingLoadDTO") or {}
    if inner:
        row.update(
            {
                "training_status": inner.get("trainingStatusFeedbackPhrase"),
                "acute_load": _num(acute.get("dailyTrainingLoadAcute")),
                "chronic_load": _num(acute.get("dailyTrainingLoadChronic")),
                "load_ratio": _num(acute.get("dailyAcuteChronicWorkloadRatio")),
                "acwr_status": acute.get("acwrStatus"),
            }
        )
    vo2 = ((status.get("mostRecentVO2Max") or {}).get("generic") or {})
    if vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"):
        row["vo2max"] = _num(vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"))

    row["updated_at"] = datetime.now(config.tz()).isoformat(timespec="seconds")
    return {k: v for k, v in row.items() if v is not None or k in ("date", "updated_at")}


def sync_activities(conn: Any, g: Any, start: str, end: str) -> int:
    """Upsert activities between two dates. Returns rows written."""
    acts = _safe(f"activities {start}..{end}", lambda: g.get_activities_by_date(start, end), []) or []
    n = 0
    for a in acts:
        aid = a.get("activityId")
        if aid is None:
            continue
        raw_type = a.get("activityType")
        type_key = raw_type.get("typeKey") if isinstance(raw_type, dict) else (raw_type or "")
        start_local = (a.get("startTimeLocal") or "")[:19]
        rec = {
            "id": str(aid),
            "date": start_local[:10] or end,
            "type": type_key,
            "name": a.get("activityName"),
            "start_local": start_local,
            "duration_s": _num(a.get("duration")),
            "moving_s": _num(a.get("movingDuration")),
            "distance_m": _num(a.get("distance")),
            "avg_hr": _num(a.get("averageHR")),
            "max_hr": _num(a.get("maxHR")),
            "calories": _num(a.get("calories")),
            "te_aerobic": _num(a.get("aerobicTrainingEffect")),
            "te_anaerobic": _num(a.get("anaerobicTrainingEffect")),
            "training_load": _num(a.get("activityTrainingLoad")),
            "cadence": _num(
                a.get("averageRunningCadenceInStepsPerMinute")
                or a.get("averageBikingCadenceInRevPerMinute")
                or a.get("averageSwimCadenceInStrokesPerMinute")
            ),
            "gct_ms": _num(a.get("avgGroundContactTime")),
            "vert_osc_cm": _num(a.get("avgVerticalOscillation")),
            "stride_cm": _num(a.get("avgStrideLength")),
            "elev_gain_m": _num(a.get("elevationGain")),
            "power_avg": _num(a.get("avgPower")),
            "power_norm": _num(a.get("normPower")),
            "raw": None,
        }
        db.upsert(conn, "activities", {k: v for k, v in rec.items() if v is not None or k == "id"}, key="id")
        n += 1
    return n


def sync_body(conn: Any, g: Any, start: str, end: str) -> int:
    """Weigh-ins → ``body`` table (Garmin reports grams)."""
    data = _safe(f"body_composition {start}..{end}", lambda: g.get_body_composition(start, end), {}) or {}
    n = 0
    for w in data.get("dateWeightList") or []:
        weight_g = _num(w.get("weight"))
        if weight_g is None:
            continue
        day = w.get("calendarDate") or (w.get("date") or end)[:10]
        ts = _ts_local(w.get("timestampGMT")) or f"{day}T08:00:00"
        weight_kg = round(weight_g / 1000.0, 2)
        exists = db.one(
            conn,
            "SELECT id FROM body WHERE date=? AND weight_kg=? AND source='garmin'",
            (day, weight_kg),
        )
        if exists:
            continue
        db.insert(
            conn,
            "body",
            {
                "ts": ts,
                "date": day,
                "weight_kg": weight_kg,
                "body_fat_pct": _num(w.get("bodyFat")),
                "muscle_kg": (lambda m: round(m / 1000.0, 2) if m else None)(_num(w.get("muscleMass"))),
                "water_pct": _num(w.get("bodyWater")),
                "source": "garmin",
                "notes": None,
            },
        )
        n += 1
    # Mirror the freshest weigh-in onto the daily row so trends need one table.
    latest = db.one(conn, "SELECT * FROM body ORDER BY date DESC, id DESC LIMIT 1")
    if latest:
        db.upsert(
            conn,
            "daily",
            {
                "date": latest["date"],
                "weight_kg": latest["weight_kg"],
                "body_fat_pct": latest["body_fat_pct"],
                "muscle_kg": latest["muscle_kg"],
            },
        )
    return n


def sync(days: int = 7, end: str | None = None, with_activities: bool = True) -> dict[str, Any]:
    """Pull the last ``days`` calendar days into the database."""
    WARN.clear()
    conn = db.connect()
    db.ensure_schema(conn)
    end_date = date_cls.fromisoformat(end) if end else datetime.now(config.tz()).date()
    start_date = end_date - timedelta(days=max(days, 1) - 1)
    start, finish = start_date.isoformat(), end_date.isoformat()

    g = client()
    written = {"daily": 0, "activities": 0, "body": 0}
    try:
        for day in db.date_range(start, finish):
            db.upsert(conn, "daily", daily_row(day, g))
            written["daily"] += 1
        written["body"] = sync_body(conn, g, start, finish)
        if with_activities:
            written["activities"] = sync_activities(conn, g, start, finish)
        # Stamp the sync so `bm fresh` can decide whether a live pull is worth it.
        db.set_meta(conn, "last_sync_at", datetime.now(config.tz()).isoformat(timespec="seconds"))
        conn.commit()
    finally:
        conn.close()

    return {
        "range": [start, finish],
        "written": written,
        "warnings": list(WARN),
    }
