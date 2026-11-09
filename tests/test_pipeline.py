from __future__ import annotations

import pytest

from bodymesh import brief, db, insights, meals, metrics


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.ensure_schema(c)
    yield c
    c.close()


def _seed_days(conn, days: int, *, rhr=48, hrv=100, sleep_h=7.8, steps=9000, load=0,
               meal_kcal=None, flat=False):
    for i in range(days):
        day = f"2026-10-{i + 1:02d}" if i < 31 else None
        if day is None:
            continue
        db.upsert(
            conn,
            "daily",
            {
                "date": day,
                "rhr": rhr + (0 if flat else i % 3),
                "hrv_ms": hrv,
                "sleep_s": int(sleep_h * 3600),
                "sleep_score": 80,
                "steps": steps,
                "total_kcal": 2800,
                "stress_avg": 25,
            },
        )
        if meal_kcal:
            meals.add_meal(
                conn=conn,
                description="test meal",
                slot="lunch",
                ts=f"{day}T12:30:00",
                kcal=meal_kcal,
                protein_g=40,
                carbs_g=80,
                fat_g=20,
                analyze=False,
            )


def test_weight_rate_from_body_table(conn):
    for i, w in enumerate([98.0, 97.6, 97.1, 96.8]):
        db.insert(conn, "body", {"ts": f"2026-11-{i + 1:02d}T08:00:00", "date": f"2026-11-{i + 1:02d}",
                                 "weight_kg": w, "source": "manual"})
    trend = metrics.weight_trend(conn, "2026-11-04", days=28)
    assert trend["latest_kg"] == 96.8
    assert trend["points"] == 4
    assert trend["rate_kg_per_week"] < -0.2  # losing weight, expressed per week


def test_energy_balance_flags_missing_intake(conn):
    _seed_days(conn, 5)
    bal = metrics.energy_balance(conn, "2026-10-05", days=5)
    assert bal["intake_kcal_per_day"] is None
    assert bal["burn_kcal_per_day"] == 2800


def test_energy_balance_computes_delta(conn):
    _seed_days(conn, 5, meal_kcal=2300)
    bal = metrics.energy_balance(conn, "2026-10-05", days=5)
    assert bal["intake_kcal_per_day"] == pytest.approx(2300, abs=1)
    assert bal["delta_kcal_per_day"] == pytest.approx(-500, abs=2)


def test_drift_finds_rhr_change(conn):
    _seed_days(conn, 20, rhr=46, flat=True)
    for i in range(8):
        db.upsert(conn, "daily", {"date": f"2026-11-{i + 1:02d}", "rhr": 52})
    found = {f["code"] for f in insights.drift_findings(conn, "2026-11-08")}
    assert "rhr_drift" in found


def test_coupling_respects_minimum_sample(conn):
    _seed_days(conn, 5)  # far below MIN_PAIRED
    assert insights.coupling_findings(conn, "2026-10-05") == []


def test_coupling_detects_a_real_sleep_rhr_link(conn):
    # strong negative relation: short sleep -> high next-day rhr
    for i in range(24):
        day = f"2026-10-{i + 1:02d}"
        hours = 5.5 + 0.1 * i  # monotonic: no periodic aliasing
        db.upsert(
            conn,
            "daily",
            {"date": day, "sleep_s": int(hours * 3600), "rhr": 62 - hours * 2, "sleep_score": 70},
        )
    found = {f["code"]: f for f in insights.coupling_findings(conn, "2026-10-24")}
    assert "sleep_vs_next_rhr" in found
    assert found["sleep_vs_next_rhr"]["evidence"]["r"] < -0.35
    assert found["sleep_vs_next_rhr"]["evidence"]["n"] >= insights.MIN_PAIRED


def test_insights_persist_and_are_deduplicated(conn):
    _seed_days(conn, 20, rhr=46, flat=True)
    for i in range(8):
        db.upsert(conn, "daily", {"date": f"2026-11-{i + 1:02d}", "rhr": 52})
    insights.run(conn, "2026-11-08", persist=True)
    insights.run(conn, "2026-11-08", persist=True)  # same date, same code -> replace
    rows = db.rows(conn, "SELECT code, COUNT(*) c FROM insights WHERE date='2026-11-08' GROUP BY code")
    assert rows
    assert all(r["c"] == 1 for r in rows)


def test_brief_renders_required_sections(conn):
    _seed_days(conn, 6, meal_kcal=2400)
    db.insert(conn, "body", {"ts": "2026-10-06T07:00:00", "date": "2026-10-06",
                             "weight_kg": 96.8, "source": "manual"})
    db.insert(
        conn,
        "activities",
        {
            "id": "1",
            "date": "2026-10-06",
            "type": "running",
            "distance_m": 6029.88,
            "duration_s": 2610.885,
            "avg_hr": 176,
            "max_hr": 184,
            "training_load": 86,
            "te_aerobic": 4.0,
        },
    )
    data = brief.build(conn, "2026-10-06")
    md = brief.render_markdown(data)
    assert "数据快照" in md
    assert "**训练**" in md
    assert "6.03 km" in md
    assert "**饮食**" in md
    assert "参考目标" in md


def test_brief_survives_an_empty_day(conn):
    md = brief.render_markdown(brief.build(conn, "2026-12-25"))
    assert "数据快照" in md
    assert "未记录" in md


def test_meal_slot_inference():
    from datetime import datetime

    assert meals.infer_slot(datetime(2026, 11, 9, 8, 0)) == "breakfast"
    assert meals.infer_slot(datetime(2026, 11, 9, 13, 0)) == "lunch"
    assert meals.infer_slot(datetime(2026, 11, 9, 19, 0)) == "dinner"
    assert meals.infer_slot(datetime(2026, 11, 9, 22, 30)) == "snack"


def test_manual_meal_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BODYMESH_DB", str(tmp_path / "m.db"))
    db.init(tmp_path / "m.db")
    out = meals.add_meal(description="牛肉面", kcal=780, protein_g=32, ts="2026-11-09T12:30:00", analyze=False)
    assert out["slot"] == "lunch"
    assert out["source"] == "manual"
    listed = meals.list_meals("2026-11-09")
    assert [m["description"] for m in listed] == ["牛肉面"]
    assert meals.delete_meal(out["id"]) is True


def test_extract_json_tolerates_code_fences():
    payload = '```json\n{"kcal": 100, "items": []}\n```'
    assert meals._extract_json(payload)["kcal"] == 100
    with pytest.raises(ValueError):
        meals._extract_json("no json here")


def test_vision_requires_a_key(monkeypatch, tmp_path):
    for var in ("BODYMESH_LLM_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError):
        meals.analyze_image(tmp_path / "x.jpg")
