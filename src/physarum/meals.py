"""Meals, weigh-ins, notes — and the food-photo estimator.

The photo path is intentionally framework-agnostic: the chat gateway only has to
hand us a file path. Estimation happens here, with a strict JSON contract, so
the assistant can be swapped without retraining the data pipeline.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, db

VISION_PROMPT = """You are a careful nutrition estimator looking at one meal photo.

Return ONLY minified JSON, no prose, matching exactly:
{"items":[{"name":"<food in English>","name_zh":"<food in Chinese>","grams":<number>,
"kcal":<number>,"protein_g":<number>,"carbs_g":<number>,"fat_g":<number>}],
"kcal":<number>,"protein_g":<number>,"carbs_g":<number>,"fat_g":<number>,"fiber_g":<number>,
"confidence":<0-1>,"notes":"<max 20 words: portion assumptions>"}

Rules:
- Estimate visible portions; prefer typical restaurant/home serving sizes.
- If the photo is unclear or not food, set confidence below 0.3 and say why in notes.
- Round grams and kcal to integers.
"""


# --------------------------------------------------------------------------
# vision
# --------------------------------------------------------------------------


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return JSON")
    return json.loads(text[start : end + 1])


def analyze_image(path: str | Path, hint: str | None = None) -> dict[str, Any]:
    """Estimate a meal from a photo using an image-capable chat model."""
    key = config.llm_key()
    if not key:
        raise RuntimeError("no model key configured (set PHYSARUM_LLM_KEY or DEEPSEEK_API_KEY)")
    img = Path(path).expanduser()
    if not img.is_file():
        raise FileNotFoundError(str(img))

    import requests

    prompt = VISION_PROMPT + (f"\nUser hint: {hint}" if hint else "")
    payload = {
        "model": config.vision_model(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_url(img)}},
                ],
            }
        ],
        "temperature": 0,
    }
    resp = requests.post(
        f"{config.llm_base()}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"vision HTTP {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    if isinstance(content, list):  # some providers return blocks
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    data = _extract_json(content)
    data["model"] = config.vision_model()
    return data


# --------------------------------------------------------------------------
# meals
# --------------------------------------------------------------------------


def infer_slot(ts: datetime) -> str:
    hour = ts.hour
    if hour < 10:
        return "breakfast"
    if hour < 15:
        return "lunch"
    if hour < 21:
        return "dinner"
    return "snack"


def add_meal(
    *,
    description: str | None = None,
    slot: str | None = None,
    ts: str | None = None,
    kcal: float | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    fiber_g: float | None = None,
    image: str | None = None,
    source: str | None = None,
    confidence: float | None = None,
    notes: str | None = None,
    analyze: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Record one meal. With an image and no macros, estimate from the photo.

    Pass ``conn`` to participate in an existing transaction (tests and batch
    imports do); otherwise a short-lived connection is opened and closed here.
    """
    own = conn is None
    if own:
        conn = db.connect()
        db.ensure_schema(conn)
    try:
        now = datetime.now(config.tz())
        when = datetime.fromisoformat(ts) if ts else now
        when = when.replace(tzinfo=when.tzinfo or config.tz())
        day = when.date().isoformat()

        items: list[dict[str, Any]] | None = None
        if image and analyze and kcal is None:
            est = analyze_image(image, hint=description)
            kcal = est.get("kcal")
            protein_g = est.get("protein_g")
            carbs_g = est.get("carbs_g")
            fat_g = est.get("fat_g")
            fiber_g = est.get("fiber_g")
            confidence = est.get("confidence", confidence)
            items = est.get("items")
            source = source or "vision"
            if not description:
                names = [i.get("name_zh") or i.get("name") for i in items or [] if i.get("name_zh") or i.get("name")]
                description = "、".join(n for n in names if n) or "未识别餐食"
            note_bits = [notes] if notes else []
            if est.get("notes"):
                note_bits.append(str(est["notes"]))
            notes = " / ".join(note_bits) or None
        elif image and kcal is not None:
            source = source or "vision+manual"
        else:
            source = source or "manual"

        if not description:
            raise ValueError("description is required when kcal is not provided")

        meal_id = db.insert(
            conn,
            "meals",
            {
                "ts": when.isoformat(timespec="seconds"),
                "date": day,
                "slot": slot or infer_slot(when),
                "description": description,
                "items": json.dumps(items, ensure_ascii=False) if items else None,
                "kcal": kcal,
                "protein_g": protein_g,
                "carbs_g": carbs_g,
                "fat_g": fat_g,
                "fiber_g": fiber_g,
                "source": source,
                "confidence": confidence,
                "image_path": str(Path(image).expanduser()) if image else None,
                "notes": notes,
                "created_at": now.isoformat(timespec="seconds"),
            },
        )
        if own:
            conn.commit()
        return {
            "id": meal_id,
            "date": day,
            "slot": slot or infer_slot(when),
            "description": description,
            "kcal": kcal,
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "confidence": confidence,
            "source": source,
            "items": items or [],
        }
    finally:
        if own:
            conn.close()


def list_meals(day: str, conn: Any = None) -> list[dict[str, Any]]:
    own = conn is None
    if own:
        conn = db.connect()
    try:
        return db.rows(conn, "SELECT * FROM meals WHERE date=? ORDER BY ts", (day,))
    finally:
        if own:
            conn.close()


def delete_meal(meal_id: int, conn: Any = None) -> bool:
    own = conn is None
    if own:
        conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM meals WHERE id=?", (meal_id,))
        if own:
            conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------
# body & notes
# --------------------------------------------------------------------------


def add_body(
    *,
    weight_kg: float | None = None,
    body_fat_pct: float | None = None,
    muscle_kg: float | None = None,
    water_pct: float | None = None,
    ts: str | None = None,
    source: str = "manual",
    notes: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    own = conn is None
    if own:
        conn = db.connect()
        db.ensure_schema(conn)
    try:
        now = datetime.now(config.tz())
        when = datetime.fromisoformat(ts) if ts else now
        day = when.date().isoformat()
        rid = db.insert(
            conn,
            "body",
            {
                "ts": when.isoformat(timespec="seconds"),
                "date": day,
                "weight_kg": weight_kg,
                "body_fat_pct": body_fat_pct,
                "muscle_kg": muscle_kg,
                "water_pct": water_pct,
                "source": source,
                "notes": notes,
            },
        )
        db.upsert(
            conn,
            "daily",
            {
                "date": day,
                "weight_kg": weight_kg,
                "body_fat_pct": body_fat_pct,
                "muscle_kg": muscle_kg,
            },
        )
        if own:
            conn.commit()
        return {"id": rid, "date": day, "weight_kg": weight_kg}
    finally:
        if own:
            conn.close()


def add_note(text: str, kind: str = "note", tags: str | None = None, ts: str | None = None) -> dict[str, Any]:
    conn = db.connect()
    db.ensure_schema(conn)
    try:
        now = datetime.now(config.tz())
        when = datetime.fromisoformat(ts) if ts else now
        rid = db.insert(
            conn,
            "notes",
            {
                "ts": when.isoformat(timespec="seconds"),
                "date": when.date().isoformat(),
                "kind": kind,
                "text": text,
                "tags": tags,
            },
        )
        conn.commit()
        return {"id": rid, "date": when.date().isoformat(), "text": text}
    finally:
        conn.close()
