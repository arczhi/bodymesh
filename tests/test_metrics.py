from __future__ import annotations

import sqlite3

import pytest

from physarum import db, metrics


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.ensure_schema(c)
    yield c
    c.close()


def test_schema_is_idempotent(conn):
    db.ensure_schema(conn)
    tables = {r["name"] for r in db.rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"daily", "activities", "meals", "body", "notes", "insights", "briefs", "meta"} <= tables


def test_upsert_daily_merges_partial_rows(conn):
    db.upsert(conn, "daily", {"date": "2026-11-08", "rhr": 50})
    db.upsert(conn, "daily", {"date": "2026-11-08", "steps": 11826})
    row = db.daily(conn, "2026-11-08")
    assert row["rhr"] == 50
    assert row["steps"] == 11826


def test_date_range_is_inclusive_and_ordered():
    assert db.date_range("2026-11-08", "2026-11-10") == ["2026-11-08", "2026-11-09", "2026-11-10"]
    assert db.date_range("2026-11-10", "2026-11-08") == ["2026-11-08", "2026-11-09", "2026-11-10"]


def test_meta_roundtrip(conn):
    db.set_meta(conn, "last_daily", {"date": "2026-11-08"})
    assert db.get_meta(conn, "last_daily") == {"date": "2026-11-08"}
    assert db.get_meta(conn, "missing", "fallback") == "fallback"


def test_unknown_column_is_rejected(conn):
    with pytest.raises(sqlite3.OperationalError):
        db.upsert(conn, "daily", {"date": "2026-11-08", "not_a_column": 1})


def test_pearson_perfect_and_degenerate():
    assert metrics.pearson([1, 2, 3, 4], [2, 4, 6, 8]) == 1.0
    assert metrics.pearson([1, 2, 3, 4], [8, 6, 4, 2]) == -1.0
    assert metrics.pearson([1, 1, 1], [1, 2, 3]) is None
    assert metrics.pearson([1, 2], [1, 2]) is None  # needs n >= 3


def test_linear_rate_handles_gaps():
    # value drops 0.5/day, sampled at day 0, 3, 7
    slope = metrics.linear_rate([(0, 97.0), (3, 95.5), (7, 93.5)])
    assert slope == pytest.approx(-0.5, abs=0.01)
    assert metrics.linear_rate([(0, 97.0)]) is None


def test_baseline_excludes_the_day_itself(conn):
    for i, rhr in enumerate([46, 47, 48, 49, 60]):
        day = f"2026-11-0{i + 1}"
        db.upsert(conn, "daily", {"date": day, "rhr": rhr})
    # 7-day baseline before 2026-11-05 must not include the 60
    assert metrics.baseline(conn, "rhr", "2026-11-05", window=7) == pytest.approx(47.5)


def test_sleep_debt_counts_only_short_nights(conn, monkeypatch):
    monkeypatch.setenv("PHYSARUM_SLEEP_NEED_H", "8")
    for i, hours in enumerate([8, 7, 6, 9]):
        db.upsert(conn, "daily", {"date": f"2026-11-0{i + 5}", "sleep_s": int(hours * 3600)})
    debt = metrics.sleep_debt(conn, "2026-11-08", days=7)
    assert debt["nights"] == 4
    assert debt["debt_hours"] == pytest.approx(3.0)  # 1h + 2h, the 9h night adds nothing


def test_rhr_signal_fires_on_a_real_jump(conn):
    for offset in range(1, 8):  # the 7 days immediately before the reading
        db.upsert(conn, "daily", {"date": f"2026-11-{offset:02d}", "rhr": 48})
    db.upsert(conn, "daily", {"date": "2026-11-08", "rhr": 54})
    codes = {s["code"] for s in metrics.recovery_signals(conn, "2026-11-08")}
    assert "rhr_up" in codes


def test_rem_signal_fires_below_share_threshold(conn):
    db.upsert(
        conn,
        "daily",
        {"date": "2026-11-09", "sleep_s": 23340, "rem_s": 1080, "sleep_score": 63},
    )
    signals = {s["code"]: s for s in metrics.recovery_signals(conn, "2026-11-09")}
    assert "rem_low" in signals


def test_activity_line_formats_a_run():
    line = metrics.activity_line(
        {
            "type": "running",
            "distance_m": 6029.88,
            "duration_s": 2610.885,
            "avg_hr": 176,
            "max_hr": 184,
            "training_load": 85.9,
            "te_aerobic": 4.0,
            "cadence": 164,
            "gct_ms": 289,
        }
    )
    assert "6.03 km" in line
    assert "43:30" in line
    assert "7'12\"/km" in line
    assert "cadence 164" in line
