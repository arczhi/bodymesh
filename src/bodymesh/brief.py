"""The morning brief: deterministic facts, ready for a model to narrate.

The brief deliberately contains **no advice**. Numbers and signals go in, a
language model (or you) decides what to do about them. That split is what keeps
the daily message honest: the same data always produces the same brief.
"""

from __future__ import annotations

from typing import Any

from . import config, db, insights, metrics


def _f(value: Any, digits: int = 0, unit: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}{unit}"
    except (TypeError, ValueError):
        return str(value)


def _pct_share(part: Any, whole: Any) -> str:
    if not part or not whole:
        return "—"
    return f"{100.0 * float(part) / float(whole):.0f}%"


def build(conn: Any, day: str) -> dict[str, Any]:
    """Assemble every number the brief needs for one calendar day."""
    row = db.daily(conn, day)
    acts = metrics.activities_on(conn, day)
    nutrition = metrics.nutrition(conn, day)
    weight = metrics.weight_trend(conn, day)
    debt = metrics.sleep_debt(conn, day)
    balance = metrics.energy_balance(conn, day)
    week = metrics.training_window(conn, day, days=7)

    signals = metrics.recovery_signals(conn, day) + metrics.nutrition_signals(conn, day)
    findings = insights.run(conn, day, persist=True)

    hours = metrics.sleep_hours(row.get("sleep_s"))
    return {
        "date": day,
        "day": row,
        "sleep_hours": hours,
        "sleep_debt": debt,
        "activities": acts,
        "activity_lines": [metrics.activity_line(a) for a in acts],
        "nutrition": {k: v for k, v in nutrition.items() if k != "items"},
        "meals": nutrition["items"],
        "weight": weight,
        "balance": balance,
        "week_load": {k: v for k, v in week.items() if k != "activities"},
        "signals": signals,
        "findings": [
            {"code": f["code"], "severity": f["severity"], "title": f["title"], "detail": f["detail"]}
            for f in findings
        ],
        "targets": _targets(weight),
    }


def _targets(weight: dict[str, Any]) -> dict[str, Any]:
    kg = weight.get("latest_kg") or config.body_weight_kg()
    if not kg:
        return {}
    return {
        "protein_g": [round(1.6 * float(kg)), round(2.2 * float(kg))],
        "sleep_hours": config.sleep_need_hours(),
    }


def render_markdown(data: dict[str, Any]) -> str:
    """Human/LLM-facing markdown. Keep it under ~45 lines."""
    row = data["day"]
    lines: list[str] = []
    lines.append(f"## 数据快照 · {data['date']}")

    overview = [
        f"步数 {_f(row.get('steps'))}",
        f"活动消耗 {_f(row.get('active_kcal'))} kcal",
        f"强度 {_f(row.get('intensity_min'))} min",
        f"压力 {_f(row.get('stress_avg'))}",
        f"静息 {_f(row.get('rhr'))} bpm",
    ]
    lines.append("**概览** " + " · ".join(overview))

    sleep_bits = [
        f"{_f(data['sleep_hours'], 1)} h",
        f"得分 {_f(row.get('sleep_score'))}（{row.get('sleep_qualifier') or '—'}）",
        f"深睡 {_pct_share(row.get('deep_s'), row.get('sleep_s'))}",
        f"REM {_pct_share(row.get('rem_s'), row.get('sleep_s'))}",
        f"夜间心率 {_f(row.get('sleep_hr'))}",
    ]
    if data["sleep_debt"].get("debt_hours"):
        sleep_bits.append(f"7 晚睡眠债 {data['sleep_debt']['debt_hours']:.1f} h")
    lines.append("**睡眠** " + " · ".join(sleep_bits))

    hrv_bits = [f"{_f(row.get('hrv_ms'))} ms", f"状态 {row.get('hrv_status') or '—'}"]
    if row.get("hrv_base_low") and row.get("hrv_base_high"):
        hrv_bits.append(f"个人平衡区间 {_f(row['hrv_base_low'])}–{_f(row['hrv_base_high'])}")
    lines.append("**HRV** " + " · ".join(hrv_bits))

    if data["activity_lines"]:
        lines.append("**训练**")
        for line in data["activity_lines"]:
            lines.append(f"- {line}")
    else:
        lines.append("**训练** 无记录")

    nut = data["nutrition"]
    if nut.get("meals"):
        lines.append(
            "**饮食** "
            + " · ".join(
                [
                    f"{nut['meals']} 餐",
                    f"{_f(nut.get('kcal'))} kcal",
                    f"蛋白 {_f(nut.get('protein_g'))} g",
                    f"碳水 {_f(nut.get('carbs_g'))} g",
                    f"脂肪 {_f(nut.get('fat_g'))} g",
                ]
            )
        )
    else:
        lines.append("**饮食** 未记录")

    weight = data["weight"]
    if weight.get("latest_kg"):
        bits = [f"{weight['latest_kg']:.2f} kg（{weight.get('latest_date')}）"]
        if weight.get("rate_kg_per_week") is not None:
            bits.append(f"趋势 {weight['rate_kg_per_week']:+.2f} kg/周")
        if weight.get("to_goal_kg") is not None:
            bits.append(f"距目标 {weight['to_goal_kg']:+.1f} kg")
        lines.append("**体重** " + " · ".join(bits))

    bal = data["balance"]
    if bal.get("delta_kcal_per_day") is not None:
        lines.append(
            f"**能量** 摄入 {_f(bal['intake_kcal_per_day'])} vs 消耗 {_f(bal['burn_kcal_per_day'])} "
            f"→ {bal['delta_kcal_per_day']:+.0f} kcal/天（近 {bal['window_days']} 天，记录 {bal['logged_days']} 天）"
        )

    week = data["week_load"]
    lines.append(
        f"**本周负荷** {week['sessions']} 次 · {week['distance_km']} km · "
        f"{_f(week['duration_min'])} min · load {_f(week['load'])}"
    )

    if data["signals"]:
        lines.append("### 身体信号")
        for s in data["signals"]:
            # ASCII markers on purpose: a brief may travel through terminals,
            # cron logs and chat gateways with wildly different locales.
            mark = {"alert": "[!]", "watch": "[~]", "info": "[.]"}.get(s["severity"], "[.]")
            lines.append(f"- {mark} {s['text']}")

    if data["findings"]:
        lines.append("### 规律与趋势")
        for f in data["findings"]:
            lines.append(f"- {f['title']}")

    targets = data.get("targets") or {}
    if targets:
        lines.append(
            f"**参考目标** 蛋白 {targets['protein_g'][0]}–{targets['protein_g'][1]} g/天 · "
            f"睡眠 {targets['sleep_hours']:.0f} h/天"
        )

    return "\n".join(lines) + "\n"


def save(conn: Any, day: str, markdown: str, model: str | None = None) -> None:
    from datetime import datetime

    db.upsert(
        conn,
        "briefs",
        {
            "date": day,
            "markdown": markdown,
            "generated_at": datetime.now(config.tz()).isoformat(timespec="seconds"),
            "model": model or "",
        },
        key="date",
    )
    conn.commit()
