"""``bm`` — the command line the assistant (and you) actually talk to.

Every command prints human text by default and machine JSON with ``--json``.
That single convention is what lets a chat agent use this as a tool surface
without an MCP server, a protocol handshake, or a framework dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

from . import brief as brief_mod
from . import config, db, garmin, insights, meals, metrics


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(config.tz()).date().isoformat()


def _yesterday() -> str:
    return (datetime.now(config.tz()).date() - timedelta(days=1)).isoformat()


def _emit(payload: Any, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(text if text is not None else json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _connect() -> Any:
    conn = db.connect()
    db.ensure_schema(conn)
    return conn


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = db.init()
    _emit({"database": str(path), "home": str(config.home())}, args.json, f"initialized {path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    result = garmin.sync(days=args.days, end=args.end, with_activities=not args.no_activities)
    text = (
        f"synced {result['range'][0]} → {result['range'][1]}: "
        f"{result['written']['daily']} days, {result['written']['activities']} activities, "
        f"{result['written']['body']} weigh-ins"
    )
    if result["warnings"]:
        text += "\nwarnings:\n" + "\n".join(f"  - {w}" for w in result["warnings"])
    _emit(result, args.json, text)
    return 0


def cmd_derive(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        day = args.date or _yesterday()
        found = insights.run(conn, day, persist=True)
    finally:
        conn.close()
    lines = [f"{f['severity']:5}  {f['title']}" for f in found] or ["no findings (data too thin)"]
    _emit(found, args.json, "\n".join(lines))
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        day = args.date or _yesterday()
        data = brief_mod.build(conn, day)
        markdown = brief_mod.render_markdown(data)
        if not args.no_save:
            brief_mod.save(conn, day, markdown)
    finally:
        conn.close()
    _emit(data if args.full else {"date": day, "markdown": markdown}, args.json, markdown)
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    """The 09:00 job: refresh, derive, brief, print. One command, idempotent."""
    day = args.date or _yesterday()
    sync_result = garmin.sync(days=args.days) if not args.no_sync else {"warnings": []}
    conn = _connect()
    try:
        data = brief_mod.build(conn, day)
        markdown = brief_mod.render_markdown(data)
        brief_mod.save(conn, day, markdown)
        db.set_meta(conn, "last_daily", {"date": day, "at": datetime.now(config.tz()).isoformat(timespec="seconds")})
        conn.commit()
    finally:
        conn.close()
    _emit(
        {"date": day, "markdown": markdown, "sync": sync_result.get("written"), "warnings": sync_result.get("warnings")},
        args.json,
        markdown,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        day = args.date or _today()
        start = metrics._shift(day, -(args.days - 1))
        rows = metrics.daily_rows(conn, start, day)
        payload = {
            "date": day,
            "today": {k: v for k, v in db.daily(conn, day).items() if v is not None},
            "last_sync": db.get_meta(conn, "last_daily"),
            "window": {
                "days": len(rows),
                "rhr_avg": metrics.safe_mean(metrics.values(rows, "rhr")),
                "hrv_avg": metrics.safe_mean(metrics.values(rows, "hrv_ms")),
                "sleep_avg_h": metrics.safe_mean(
                    [h for h in (metrics.sleep_hours(r.get("sleep_s")) for r in rows) if h]
                ),
                "steps_avg": metrics.safe_mean(metrics.values(rows, "steps")),
            },
            "weight": metrics.weight_trend(conn, day),
            "week_load": {k: v for k, v in metrics.training_window(conn, day, 7).items() if k != "activities"},
            "signals": metrics.recovery_signals(conn, day),
        }
    finally:
        conn.close()
    if args.json:
        _emit(payload, True)
        return 0
    w = payload["window"]
    lines = [
        f"{day}  rhr {w['rhr_avg'] or '—'}  hrv {w['hrv_avg'] or '—'} ms  "
        f"睡眠 {w['sleep_avg_h'] or '—'} h  步数 {w['steps_avg'] or '—'}",
        f"体重 {payload['weight'].get('latest_kg') or '—'} kg  "
        f"趋势 {payload['weight'].get('rate_kg_per_week') or '—'} kg/周",
        f"本周 {payload['week_load']['sessions']} 次 / {payload['week_load']['distance_km']} km",
    ]
    for s in payload["signals"]:
        lines.append(f"  [{'!' if s['severity'] == 'alert' else '·'}] {s['text']}")
    _emit(payload, False, "\n".join(lines))
    return 0


def cmd_meal(args: argparse.Namespace) -> int:
    if args.action == "list":
        day = args.date or _today()
        items = meals.list_meals(day)
        text = "\n".join(
            f"{m['ts'][11:16]}  {m['slot']:9}  {m['description'][:40]:40}  "
            f"{(m['kcal'] or 0):.0f} kcal  P{(m['protein_g'] or 0):.0f}"
            for m in items
        ) or "no meals logged"
        _emit(items, args.json, text)
        return 0
    if args.action == "rm":
        ok = meals.delete_meal(args.id)
        _emit({"deleted": ok, "id": args.id}, args.json, "deleted" if ok else "not found")
        return 0
    # add
    meal = meals.add_meal(
        description=args.description,
        slot=args.slot,
        ts=args.ts,
        kcal=args.kcal,
        protein_g=args.protein,
        carbs_g=args.carbs,
        fat_g=args.fat,
        fiber_g=args.fiber,
        image=args.image,
        notes=args.notes,
        confidence=args.confidence,
    )
    text = (
        f"{meal['slot']} · {meal['description']} · {meal['kcal'] or '—'} kcal"
        f" (蛋白 {meal['protein_g'] or '—'} g, 碳水 {meal['carbs_g'] or '—'} g, 脂肪 {meal['fat_g'] or '—'} g)"
    )
    if meal.get("confidence") is not None:
        text += f" · 置信度 {meal['confidence']}"
    _emit(meal, args.json, text)
    return 0


def cmd_body(args: argparse.Namespace) -> int:
    out = meals.add_body(
        weight_kg=args.weight,
        body_fat_pct=args.fat,
        muscle_kg=args.muscle,
        water_pct=args.water,
        ts=args.ts,
        notes=args.notes,
    )
    _emit(out, args.json, f"recorded {out['weight_kg']} kg on {out['date']}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    out = meals.add_note(args.text, kind=args.kind, tags=args.tags, ts=args.ts)
    _emit(out, args.json, f"noted [{out['date']}] {out['text']}")
    return 0


def cmd_trend(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        day = args.date or _today()
        start = metrics._shift(day, -(args.days - 1))
        rows = metrics.daily_rows(conn, start, day)
        series = [(r["date"], r.get(args.metric)) for r in rows if r.get(args.metric) is not None]
    finally:
        conn.close()
    if args.json:
        _emit({"metric": args.metric, "series": series}, True)
        return 0
    if not series:
        print(f"no data for {args.metric}")
        return 0
    lo = min(v for _, v in series)
    hi = max(v for _, v in series)
    for day_, value in series:
        span = 24
        width = 0 if hi == lo else int(round((value - lo) / (hi - lo) * span))
        print(f"{day_}  {value:>8.2f}  {'▇' * max(width, 1)}")
    return 0


def cmd_insights(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        items = insights.recent(conn, days=args.days)
    finally:
        conn.close()
    text = "\n".join(f"{i['date']}  {i['severity']:5}  {i['title']}" for i in items) or "no stored insights"
    _emit(items, args.json, text)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: dict[str, Any] = {}
    checks["python"] = sys.version.split()[0]
    checks["home"] = str(config.home())
    checks["database"] = {"path": str(config.db_path()), "exists": config.db_path().exists()}
    checks["garmin_tokens"] = {
        "path": str(config.garmin_token_dir()),
        "exists": (config.garmin_token_dir() / "garmin_tokens.json").exists(),
        "is_cn": config.garmin_is_cn(),
    }
    checks["model_key"] = {"present": bool(config.llm_key()), "base": config.llm_base()}
    conn = _connect()
    try:
        checks["counts"] = {
            "daily": db.scalar(conn, "SELECT COUNT(*) FROM daily"),
            "activities": db.scalar(conn, "SELECT COUNT(*) FROM activities"),
            "meals": db.scalar(conn, "SELECT COUNT(*) FROM meals"),
            "body": db.scalar(conn, "SELECT COUNT(*) FROM body"),
            "insights": db.scalar(conn, "SELECT COUNT(*) FROM insights"),
        }
        checks["latest"] = db.one(conn, "SELECT * FROM daily ORDER BY date DESC LIMIT 1")
    finally:
        conn.close()
    ok = checks["garmin_tokens"]["exists"] and checks["database"]["exists"]
    checks["ok"] = bool(ok)
    _emit(
        checks,
        args.json,
        "\n".join(
            [
                f"python      {checks['python']}",
                f"database    {checks['database']['path']} ({'ok' if checks['database']['exists'] else 'new'})",
                f"garmin      tokens {'found' if checks['garmin_tokens']['exists'] else 'MISSING'} "
                f"({checks['garmin_tokens']['path']}, cn={checks['garmin_tokens']['is_cn']})",
                f"model key   {'set' if checks['model_key']['present'] else 'MISSING'} ({checks['model_key']['base']})",
                "counts      " + ", ".join(f"{k}={v}" for k, v in checks["counts"].items()),
                f"latest day  {(checks['latest'] or {}).get('date', '—')}",
            ]
        ),
    )
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bm", description="Bodymesh: every number your body produces, woven into one mesh")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="machine-readable output (works before or after the verb)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="create the database", parents=[common])
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("sync", help="pull Garmin data", parents=[common])
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--end", default=None, help="ISO date, defaults to today")
    sp.add_argument("--no-activities", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("derive", help="recompute findings for a date", parents=[common])
    sp.add_argument("--date", default=None)
    sp.set_defaults(func=cmd_derive)

    sp = sub.add_parser("brief", help="render the markdown brief", parents=[common])
    sp.add_argument("--date", default=None)
    sp.add_argument("--full", action="store_true", help="include the full structured payload")
    sp.add_argument("--no-save", action="store_true")
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("daily", help="sync + derive + brief (the 09:00 job)", parents=[common])
    sp.add_argument("--date", default=None)
    sp.add_argument("--days", type=int, default=3)
    sp.add_argument("--no-sync", action="store_true")
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("status", help="compact snapshot", parents=[common])
    sp.add_argument("--date", default=None)
    sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("meal", help="log or list meals", parents=[common])
    meal_sub = sp.add_subparsers(dest="action", required=True)
    add = meal_sub.add_parser("add", parents=[common])
    add.add_argument("--description", "-d", default=None)
    add.add_argument("--slot", default=None, choices=["breakfast", "lunch", "dinner", "snack"])
    add.add_argument("--ts", default=None)
    add.add_argument("--kcal", type=float, default=None)
    add.add_argument("--protein", type=float, default=None)
    add.add_argument("--carbs", type=float, default=None)
    add.add_argument("--fat", type=float, default=None)
    add.add_argument("--fiber", type=float, default=None)
    add.add_argument("--image", default=None, help="photo path; estimated when no macros given")
    add.add_argument("--confidence", type=float, default=None)
    add.add_argument("--notes", default=None)
    lst = meal_sub.add_parser("list", parents=[common])
    lst.add_argument("--date", default=None)
    rm = meal_sub.add_parser("rm", parents=[common])
    rm.add_argument("--id", type=int, required=True)
    sp.set_defaults(func=cmd_meal)

    sp = sub.add_parser("body", help="record a weigh-in", parents=[common])
    bsub = sp.add_subparsers(dest="body_action", required=True)
    badd = bsub.add_parser("add", parents=[common])
    badd.add_argument("--weight", type=float, default=None)
    badd.add_argument("--fat", type=float, default=None)
    badd.add_argument("--muscle", type=float, default=None)
    badd.add_argument("--water", type=float, default=None)
    badd.add_argument("--ts", default=None)
    badd.add_argument("--notes", default=None)
    badd.set_defaults(func=cmd_body)

    sp = sub.add_parser("note", help="free-text note", parents=[common])
    nsub = sp.add_subparsers(dest="note_action", required=True)
    nadd = nsub.add_parser("add", parents=[common])
    nadd.add_argument("text")
    nadd.add_argument("--kind", default="note")
    nadd.add_argument("--tags", default=None)
    nadd.add_argument("--ts", default=None)
    nadd.set_defaults(func=cmd_note)

    sp = sub.add_parser("trend", help="ASCII trend for one metric", parents=[common])
    sp.add_argument("--metric", required=True)
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--date", default=None)
    sp.set_defaults(func=cmd_trend)

    sp = sub.add_parser("insights", help="stored findings", parents=[common])
    sp.add_argument("--days", type=int, default=14)
    sp.set_defaults(func=cmd_insights)

    sp = sub.add_parser("doctor", help="environment self-check", parents=[common])
    sp.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
