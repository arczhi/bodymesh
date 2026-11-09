"""Pattern finder: turns rows into statements worth reading.

Two kinds of findings:

* **Drift** – a rolling window compared with the longer baseline (recovery,
  sleep, weight, protein, energy balance).
* **Coupling** – do two things move together? Pearson r on paired daily series,
  reported only when the sample is large enough to be worth a sentence, always
  with ``n`` and ``r`` so a human can dismiss it.

Nothing here guesses. If the data is thin, the finding is not emitted — silence
beats a confident hallucination about somebody's body.
"""

from __future__ import annotations

from typing import Any

from . import config, db, metrics

KJ_PER_KG_FAT = 7700.0
MIN_PAIRED = 10
MIN_R = 0.35


def _window(conn: Any, end: str, days: int) -> list[dict[str, Any]]:
    start = metrics._shift(end, -(days - 1))
    return db.rows(
        conn,
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM meals m WHERE m.date=d.date) AS meal_count, "
        "(SELECT SUM(m.kcal) FROM meals m WHERE m.date=d.date) AS meal_kcal, "
        "(SELECT SUM(m.protein_g) FROM meals m WHERE m.date=d.date) AS meal_protein, "
        "(SELECT MAX(m.ts) FROM meals m WHERE m.date=d.date) AS last_meal_ts, "
        "COALESCE((SELECT SUM(a.training_load) FROM activities a WHERE a.date=d.date),0) AS day_load "
        "FROM daily d WHERE d.date BETWEEN ? AND ? ORDER BY d.date",
        (start, end),
    )


def _paired(rows: list[dict[str, Any]], x_key: str, y_key: str, shift: int = 1) -> tuple[list[float], list[float]]:
    """Pair ``x`` on day i with ``y`` on day i+``shift``."""
    xs: list[float] = []
    ys: list[float] = []
    for i in range(len(rows) - shift):
        a = rows[i]
        b = rows[i + shift]
        if a.get(x_key) is None or b.get(y_key) is None:
            continue
        xs.append(float(a[x_key]))
        ys.append(float(b[y_key]))
    return xs, ys


def _finding(code: str, severity: str, title: str, detail: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "evidence": evidence,
    }


# --------------------------------------------------------------------------
# individual analyses
# --------------------------------------------------------------------------


def drift_findings(conn: Any, as_of: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    recent = _window(conn, as_of, 7)
    longer = _window(conn, as_of, 28)

    # resting heart rate
    r7 = metrics.safe_mean(metrics.values(recent, "rhr"))
    r28 = metrics.safe_mean(metrics.values(longer, "rhr"))
    if r7 is not None and r28 is not None:
        delta = round(r7 - r28, 1)
        if abs(delta) >= 3:
            out.append(
                _finding(
                    "rhr_drift",
                    "watch" if delta > 0 else "info",
                    f"静息心率近 7 天 {r7:.0f}，28 天 {r28:.0f} bpm",
                    ("升高" if delta > 0 else "下降") + f" {abs(delta):.1f} bpm",
                    {"rhr_7d": r7, "rhr_28d": r28, "n": len(metrics.values(longer, "rhr"))},
                )
            )

    # hrv
    h7 = metrics.safe_mean(metrics.values(recent, "hrv_ms"))
    h28 = metrics.safe_mean(metrics.values(longer, "hrv_ms"))
    if h7 is not None and h28 is not None and h28 > 0:
        delta_pct = round(100.0 * (h7 - h28) / h28, 1)
        if abs(delta_pct) >= 8:
            out.append(
                _finding(
                    "hrv_drift",
                    "watch" if delta_pct < 0 else "info",
                    f"HRV 近 7 天 {h7:.0f} ms，28 天 {h28:.0f} ms",
                    f"变化 {delta_pct:+.1f}%",
                    {"hrv_7d": h7, "hrv_28d": h28},
                )
            )

    # sleep
    s7 = metrics.safe_mean([metrics.sleep_hours(r["sleep_s"]) for r in recent if r.get("sleep_s")])
    s28 = metrics.safe_mean([metrics.sleep_hours(r["sleep_s"]) for r in longer if r.get("sleep_s")])
    if s7 is not None and s28 is not None:
        delta = round(s7 - s28, 2)
        if abs(delta) >= 0.5:
            out.append(
                _finding(
                    "sleep_drift",
                    "watch" if delta < 0 else "info",
                    f"睡眠近 7 晚平均 {s7:.1f} h，28 晚 {s28:.1f} h",
                    f"{delta:+.1f} h/晚",
                    {"sleep_7d_h": s7, "sleep_28d_h": s28},
                )
            )

    # weight + energy balance cross-check
    trend = metrics.weight_trend(conn, as_of, days=28)
    rate = trend.get("rate_kg_per_week")
    balance = metrics.energy_balance(conn, as_of, days=14)
    delta_kcal = balance.get("delta_kcal_per_day")
    if rate is not None and delta_kcal is not None and balance.get("logged_days", 0) >= 5:
        expected = round(delta_kcal * 7 / KJ_PER_KG_FAT, 2)
        gap = round(rate - expected, 2)
        out.append(
            _finding(
                "weight_vs_balance",
                "info",
                f"体重趋势 {rate:+.2f} kg/周（实测），{expected:+.2f} kg/周（按热量缺口推算）",
                f"两者相差 {gap:+.2f} kg/周；差距大通常意味着摄入记录不全或消耗被高估",
                {
                    "observed_kg_per_week": rate,
                    "expected_kg_per_week": expected,
                    "delta_kcal_per_day": delta_kcal,
                    "logged_days": balance.get("logged_days"),
                    "weight_points": trend.get("points"),
                },
            )
        )
    elif rate is not None and abs(rate) >= 0.05:
        out.append(
            _finding(
                "weight_drift",
                "info",
                f"体重趋势 {rate:+.2f} kg/周",
                f"近 28 天 {trend.get('points')} 次称重",
                {"rate_kg_per_week": rate, "latest_kg": trend.get("latest_kg")},
            )
        )

    # protein density
    nut = metrics.nutrition_window(conn, metrics._shift(as_of, -13), as_of)
    weight = trend.get("latest_kg") or config.body_weight_kg()
    if nut.get("protein_g_per_day") and weight and nut.get("logged_days", 0) >= 5:
        per_kg = round(nut["protein_g_per_day"] / float(weight), 2)
        if per_kg < 1.4 or per_kg > 2.4:
            out.append(
                _finding(
                    "protein_density",
                    "watch" if per_kg < 1.4 else "info",
                    f"蛋白质 {per_kg:.2f} g/kg/天",
                    "减脂期目标 1.6–2.2 g/kg，过低会加速肌肉流失"
                    if per_kg < 1.4
                    else "高于常规区间，注意肾脏与总热量负担",
                    {"g_per_kg": per_kg, "g_per_day": nut["protein_g_per_day"], "weight_kg": weight},
                )
            )

    return out


def coupling_findings(conn: Any, as_of: str) -> list[dict[str, Any]]:
    """Do two behaviours move together? Only reported with enough pairs."""
    out: list[dict[str, Any]] = []
    rows = _window(conn, as_of, 60)

    pairs: list[tuple[str, str, int, str, str]] = [
        ("sleep_s", "rhr", 1, "sleep_vs_next_rhr", "睡眠时长 → 次日静息心率"),
        ("day_load", "sleep_score", 1, "load_vs_next_sleep", "当天训练负荷 → 次夜睡眠分"),
        ("steps", "rhr", 1, "steps_vs_next_rhr", "每天步数 → 次日静息心率"),
        ("meal_kcal", "weight_kg", 1, "intake_vs_next_weight", "当天摄入热量 → 次日体重"),
        ("stress_avg", "sleep_score", 1, "stress_vs_next_sleep", "当天平均压力 → 次夜睡眠分"),
    ]
    for x_key, y_key, shift, code, label in pairs:
        xs, ys = _paired(rows, x_key, y_key, shift)
        if len(xs) < MIN_PAIRED:
            continue
        if x_key == "sleep_s":
            xs = [v / 3600.0 for v in xs]
        r = metrics.pearson(xs, ys)
        if r is None or abs(r) < MIN_R:
            continue
        direction = "正相关" if r > 0 else "负相关"
        out.append(
            _finding(
                code,
                "info",
                f"{label}：{direction} r={r:+.2f}（n={len(xs)}）",
                "相关性不等于因果，只是值得继续观察的线索",
                {"r": r, "n": len(xs), "x": x_key, "y": y_key, "shift_days": shift},
            )
        )

    # late meals vs next-night sleep
    late: list[float] = []
    early: list[float] = []
    for i in range(len(rows) - 1):
        ts = rows[i].get("last_meal_ts")
        nxt = rows[i + 1].get("sleep_score")
        if not ts or nxt is None:
            continue
        hour = int((ts or "T00:00")[11:13] or 0)
        (late if hour >= 21 else early).append(float(nxt))
    if len(late) >= 5 and len(early) >= 5:
        ml, me = metrics.safe_mean(late), metrics.safe_mean(early)
        diff = round(ml - me, 1)
        if abs(diff) >= 3:
            out.append(
                _finding(
                    "late_meal_vs_sleep",
                    "info",
                    f"21 点后进食的夜晚睡眠分 {ml:.0f}，早吃饭的夜晚 {me:.0f}",
                    f"相差 {diff:+.1f} 分（晚睡组 n={len(late)}，早吃组 n={len(early)}）",
                    {"late_mean": ml, "early_mean": me, "late_n": len(late), "early_n": len(early)},
                )
            )

    return out


def run(conn: Any, as_of: str, persist: bool = True) -> list[dict[str, Any]]:
    """Compute all findings for ``as_of`` and optionally store them."""
    found = drift_findings(conn, as_of) + coupling_findings(conn, as_of)
    if persist:
        from datetime import datetime

        now = datetime.now(config.tz()).isoformat(timespec="seconds")
        for f in found:
            db.insert(
                conn,
                "insights",
                {
                    "date": as_of,
                    "code": f["code"],
                    "severity": f["severity"],
                    "title": f["title"],
                    "detail": f["detail"],
                    "evidence": _json(f["evidence"]),
                    "created_at": now,
                },
            )
        conn.commit()
    return found


def recent(conn: Any, days: int = 14) -> list[dict[str, Any]]:
    return db.rows(
        conn,
        "SELECT * FROM insights WHERE date >= date('now', ?) ORDER BY date DESC, "
        "CASE severity WHEN 'alert' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END",
        (f"-{days} days",),
    )


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
