"""Derived layer: statistics, rolling baselines and plain-language signals.

Everything here is deterministic and unit-testable. The language model never
computes a number — it only narrates what this module found. That keeps the
morning brief reproducible and cheap.
"""

from __future__ import annotations

import math
from statistics import mean, median, pstdev
from typing import Any, Iterable, Sequence

from . import config, db

Num = float | int | None


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def values(records: Iterable[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for r in records:
        v = r.get(key)
        if v is not None:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
    return out


def safe_mean(xs: Sequence[float]) -> float | None:
    return round(mean(xs), 2) if xs else None


def safe_median(xs: Sequence[float]) -> float | None:
    return round(median(xs), 2) if xs else None


def safe_std(xs: Sequence[float]) -> float | None:
    return round(pstdev(xs), 2) if len(xs) > 1 else None


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson r for two equal-length series. ``None`` if degenerate."""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = list(xs[:n]), list(ys[:n])
    mx, my = mean(xs), mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 3)


def linear_rate(points: Sequence[tuple[float, float]]) -> float | None:
    """Least-squares slope of y over x (x in days). Robust to gaps."""
    n = len(points)
    if n < 2:
        return None
    mx = mean(p[0] for p in points)
    my = mean(p[1] for p in points)
    denom = sum((p[0] - mx) ** 2 for p in points)
    if denom == 0:
        return None
    num = sum((p[0] - mx) * (p[1] - my) for p in points)
    return num / denom


def pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


# --------------------------------------------------------------------------
# series helpers
# --------------------------------------------------------------------------


def daily_rows(conn: Any, start: str, end: str) -> list[dict[str, Any]]:
    return db.rows(conn, "SELECT * FROM daily WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))


def day_offsets(records: Sequence[dict[str, Any]]) -> list[tuple[float, float]]:
    """(day index, value) pairs for records that have a numeric ``value``."""
    out: list[tuple[float, float]] = []
    if not records:
        return out
    base = records[0]["date"]
    from datetime import date as d

    b = d.fromisoformat(base)
    for r in records:
        v = r.get("value")
        if v is None:
            continue
        out.append(((d.fromisoformat(r["date"]) - b).days, float(v)))
    return out


def baseline(conn: Any, column: str, as_of: str, window: int = 7, exclude_self: bool = True) -> float | None:
    """Mean of ``column`` over the ``window`` days *before* ``as_of``."""
    from datetime import date as d

    end = d.fromisoformat(as_of)
    if exclude_self:
        end = d.fromordinal(end.toordinal() - 1)
    start = d.fromordinal(end.toordinal() - (window - 1))
    rows = db.rows(
        conn,
        f"SELECT {column} AS value FROM daily WHERE date BETWEEN ? AND ? AND {column} IS NOT NULL",
        (start.isoformat(), end.isoformat()),
    )
    return safe_mean(values(rows, "value"))


# --------------------------------------------------------------------------
# domain rollups
# --------------------------------------------------------------------------


def sleep_hours(seconds: Num) -> float | None:
    return round(float(seconds) / 3600.0, 2) if seconds else None


def weight_trend(conn: Any, as_of: str, days: int = 28) -> dict[str, Any]:
    from datetime import date as d

    end = d.fromisoformat(as_of)
    start = d.fromordinal(end.toordinal() - days + 1).isoformat()
    rows = db.rows(
        conn,
        "SELECT date, weight_kg AS value FROM body WHERE date BETWEEN ? AND ? AND weight_kg IS NOT NULL "
        "ORDER BY date, id",
        (start, as_of),
    )
    # keep the last reading per day
    per_day: dict[str, float] = {}
    for r in rows:
        per_day[r["date"]] = float(r["value"])
    series = [{"date": k, "value": v} for k, v in sorted(per_day.items())]
    slope = linear_rate(day_offsets(series))
    latest = series[-1] if series else None
    week_ago = [s for s in series if s["date"] <= _shift(as_of, -7)]
    month_ago = series[0] if series else None
    goal = config.goal_weight_kg()
    return {
        "latest_kg": latest["value"] if latest else None,
        "latest_date": latest["date"] if latest else None,
        "week_ago_kg": week_ago[-1]["value"] if week_ago else None,
        "month_ago_kg": month_ago["value"] if month_ago else None,
        "rate_kg_per_week": round(slope * 7, 2) if slope is not None else None,
        "points": len(series),
        "goal_kg": goal,
        "to_goal_kg": round(latest["value"] - goal, 2) if latest and goal else None,
    }


def _shift(day: str, delta: int) -> str:
    from datetime import date as d

    return d.fromordinal(d.fromisoformat(day).toordinal() + delta).isoformat()


def nutrition(conn: Any, day: str) -> dict[str, Any]:
    rows = db.rows(conn, "SELECT * FROM meals WHERE date=? ORDER BY ts", (day,))
    return {
        "meals": len(rows),
        "kcal": _sum(rows, "kcal"),
        "protein_g": _sum(rows, "protein_g"),
        "carbs_g": _sum(rows, "carbs_g"),
        "fat_g": _sum(rows, "fat_g"),
        "fiber_g": _sum(rows, "fiber_g"),
        "items": rows,
    }


def _sum(rows: Sequence[dict[str, Any]], key: str) -> float | None:
    xs = values(rows, key)
    return round(sum(xs), 1) if xs else None


def nutrition_window(conn: Any, start: str, end: str) -> dict[str, Any]:
    rows = db.rows(conn, "SELECT * FROM meals WHERE date BETWEEN ? AND ?", (start, end))
    days = len({r["date"] for r in rows})
    return {
        "logged_days": days,
        "meals": len(rows),
        "kcal_per_day": round(_sum(rows, "kcal") / days, 0) if days and _sum(rows, "kcal") else None,
        "protein_g_per_day": round(_sum(rows, "protein_g") / days, 0) if days and _sum(rows, "protein_g") else None,
        "carbs_g_per_day": round(_sum(rows, "carbs_g") / days, 0) if days and _sum(rows, "carbs_g") else None,
        "fat_g_per_day": round(_sum(rows, "fat_g") / days, 0) if days and _sum(rows, "fat_g") else None,
    }


def energy_balance(conn: Any, end: str, days: int = 14) -> dict[str, Any]:
    """Intake vs Garmin's claimed burn. Both sides are estimates — see note."""
    start = _shift(end, -(days - 1))
    nutrition_days = nutrition_window(conn, start, end)
    burn_rows = db.rows(
        conn,
        "SELECT total_kcal AS value FROM daily WHERE date BETWEEN ? AND ? AND total_kcal IS NOT NULL "
        "AND date < ?",
        (start, end, end),
    )
    burn = safe_mean(values(burn_rows, "value"))
    intake = nutrition_days["kcal_per_day"]
    delta = round(intake - burn, 0) if intake is not None and burn is not None else None
    return {
        "window_days": days,
        "intake_kcal_per_day": intake,
        "burn_kcal_per_day": burn,
        "delta_kcal_per_day": delta,
        "logged_days": nutrition_days["logged_days"],
        "note": "burn is Garmin's total daily energy; intake only counts logged meals",
    }


def training_window(conn: Any, end: str, days: int = 7) -> dict[str, Any]:
    start = _shift(end, -(days - 1))
    acts = db.rows(
        conn,
        "SELECT * FROM activities WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end),
    )
    distance = sum((a.get("distance_m") or 0) for a in acts)
    duration = sum((a.get("duration_s") or 0) for a in acts)
    load = sum((a.get("training_load") or 0) for a in acts)
    return {
        "sessions": len(acts),
        "distance_km": round(distance / 1000.0, 2),
        "duration_min": round(duration / 60.0, 0),
        "load": round(load, 0) if load else 0,
        "activities": acts,
    }


def activities_on(conn: Any, day: str) -> list[dict[str, Any]]:
    return db.rows(conn, "SELECT * FROM activities WHERE date=? ORDER BY start_local", (day,))


def sleep_debt(conn: Any, end: str, days: int = 7) -> dict[str, Any]:
    start = _shift(end, -(days - 1))
    rows = db.rows(
        conn,
        "SELECT date, sleep_s FROM daily WHERE date BETWEEN ? AND ? AND sleep_s IS NOT NULL",
        (start, end),
    )
    need = config.sleep_need_hours()
    hours = [r["sleep_s"] / 3600.0 for r in rows]
    nightly_need = need
    debt = round(sum(max(0.0, nightly_need - h) for h in hours), 2) if hours else None
    return {
        "nights": len(hours),
        "avg_hours": round(mean(hours), 2) if hours else None,
        "debt_hours": debt,
        "need_hours": nightly_need,
    }


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------


def _fmt(value: float | None, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "—"
    if digits == 0:
        return f"{value:.0f}{unit}"
    return f"{value:.{digits}f}{unit}"


def recovery_signals(conn: Any, day: str) -> list[dict[str, str]]:
    """Rule-based read of recovery state. Severity: info | watch | alert."""
    row = db.daily(conn, day)
    out: list[dict[str, str]] = []

    rhr = row.get("rhr")
    rhr_base = baseline(conn, "rhr", day, window=7)
    if rhr is not None and rhr_base is not None:
        delta = rhr - rhr_base
        if delta >= 5:
            out.append(
                {
                    "code": "rhr_up",
                    "severity": "alert",
                    "text": f"静息心率 {_fmt(rhr)} 比近 7 日均值 {_fmt(rhr_base)} 高 {_fmt(delta)} 次/分",
                }
            )
        elif delta >= 3:
            out.append(
                {
                    "code": "rhr_up_mild",
                    "severity": "watch",
                    "text": f"静息心率 {_fmt(rhr)}，比 7 日均值高 {_fmt(delta)} 次/分",
                }
            )

    hrv = row.get("hrv_ms")
    if (row.get("hrv_status") or "").upper() in {"UNBALANCED", "LOW", "POOR"}:
        out.append(
            {
                "code": "hrv_unbalanced",
                "severity": "watch",
                "text": f"HRV 状态 {row.get('hrv_status')}（昨夜 {_fmt(hrv, ' ms')}）",
            }
        )
    elif hrv is not None and row.get("hrv_base_low") and hrv < float(row["hrv_base_low"]):
        out.append(
            {
                "code": "hrv_low",
                "severity": "watch",
                "text": f"HRV {_fmt(hrv, ' ms')} 低于个人平衡区间下沿 {_fmt(row['hrv_base_low'], ' ms')}",
            }
        )

    hours = sleep_hours(row.get("sleep_s"))
    need = config.sleep_need_hours()
    if hours is not None:
        if hours < need - 1.5:
            out.append(
                {
                    "code": "sleep_short",
                    "severity": "alert",
                    "text": f"睡眠仅 {hours:.1f} 小时，比目标 {need:.0f} 小时少 {need - hours:.1f} 小时",
                }
            )
        elif hours < need - 0.75:
            out.append(
                {
                    "code": "sleep_short_mild",
                    "severity": "watch",
                    "text": f"睡眠 {hours:.1f} 小时，略短于目标 {need:.0f} 小时",
                }
            )
        rem = row.get("rem_s")
        if rem and row.get("sleep_s"):
            share = 100.0 * rem / row["sleep_s"]
            if share < 15:
                out.append(
                    {
                        "code": "rem_low",
                        "severity": "watch",
                        "text": f"REM 睡眠仅占 {share:.0f}%（健康区间 21–31%），常见于睡太短、酒精或交感神经未放松",
                    }
                )
    if (row.get("sleep_score") or 100) < 70:
        out.append(
            {
                "code": "sleep_score_low",
                "severity": "watch",
                "text": f"睡眠得分 {_fmt(row.get('sleep_score'))}（{row.get('sleep_qualifier') or '—'}）",
            }
        )

    debt = sleep_debt(conn, day, days=7)
    if debt["debt_hours"] and debt["debt_hours"] >= 4:
        out.append(
            {
                "code": "sleep_debt",
                "severity": "watch",
                "text": f"近 7 晚累计睡眠债 {debt['debt_hours']:.1f} 小时",
            }
        )

    ratio = row.get("load_ratio")
    if ratio is not None:
        if ratio >= 1.5:
            out.append(
                {
                    "code": "load_spike",
                    "severity": "watch",
                    "text": f"急慢性负荷比 {ratio:.2f}（>1.5 属于骤增区间）",
                }
            )
        elif ratio < 0.6 and (row.get("chronic_load") or 0) >= 80:
            out.append(
                {
                    "code": "load_low",
                    "severity": "info",
                    "text": f"负荷比 {ratio:.2f}，近期训练量偏低",
                }
            )

    if (row.get("stress_avg") or 0) >= 45:
        out.append(
            {
                "code": "stress_high",
                "severity": "watch",
                "text": f"全天平均压力 {_fmt(row.get('stress_avg'))}（≥45 偏高）",
            }
        )
    if row.get("bb_low") is not None and row["bb_low"] <= 15:
        out.append(
            {
                "code": "bb_low",
                "severity": "info",
                "text": f"身体电量最低跌到 {_fmt(row['bb_low'])}，说明当天没有真正的空档恢复",
            }
        )

    return out


def nutrition_signals(conn: Any, day: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    n = nutrition(conn, day)
    if not n["meals"]:
        out.append(
            {
                "code": "no_meals",
                "severity": "info",
                "text": "昨天没有记录饮食，能量平衡无法计算",
            }
        )
        return out

    weight = db.scalar(conn, "SELECT weight_kg FROM body ORDER BY date DESC, id DESC LIMIT 1") or config.body_weight_kg()
    protein = n.get("protein_g")
    if weight and protein:
        per_kg = protein / float(weight)
        if per_kg < 1.2:
            out.append(
                {
                    "code": "protein_low",
                    "severity": "watch",
                    "text": f"蛋白质 {protein:.0f} g ≈ {per_kg:.1f} g/kg，减脂期建议 1.6–2.2 g/kg",
                }
            )
    burn = db.daily(conn, day).get("total_kcal")
    if burn and n.get("kcal"):
        delta = n["kcal"] - float(burn)
        if delta <= -900:
            out.append(
                {
                    "code": "deficit_large",
                    "severity": "watch",
                    "text": f"摄入 {n['kcal']:.0f} kcal，比消耗低 {-delta:.0f} kcal，缺口偏大（易掉肌肉、影响恢复）",
                }
            )
        elif delta > 300:
            out.append(
                {
                    "code": "surplus",
                    "severity": "info",
                    "text": f"摄入 {n['kcal']:.0f} kcal，比消耗高 {delta:.0f} kcal",
                }
            )
    return out


def activity_line(act: dict[str, Any]) -> str:
    """One-line human summary of an activity."""
    type_key = (act.get("type") or "").lower()
    distance = act.get("distance_m") or 0
    duration = act.get("duration_s") or 0
    bits: list[str] = [f"{act.get('type') or 'activity'}"]
    if distance and duration:
        pace_s = duration / (distance / 1000.0)
        bits.append(f"{distance / 1000:.2f} km")
        bits.append(f"{int(duration // 60)}:{int(duration % 60):02d}")
        bits.append(f"{int(pace_s // 60)}'{int(pace_s % 60):02d}\"/km")
    elif duration:
        bits.append(f"{int(duration // 60)} min")
    if act.get("avg_hr"):
        bits.append(f"avg {act['avg_hr']:.0f} bpm")
    if act.get("max_hr"):
        bits.append(f"max {act['max_hr']:.0f}")
    if act.get("training_load"):
        bits.append(f"load {act['training_load']:.0f}")
    if act.get("te_aerobic"):
        bits.append(f"TE {act['te_aerobic']:.1f}")
    if type_key.startswith(("running", "treadmill")):
        if act.get("cadence"):
            bits.append(f"cadence {act['cadence']:.0f}")
        if act.get("gct_ms"):
            bits.append(f"GCT {act['gct_ms']:.0f} ms")
    return " · ".join(bits)
