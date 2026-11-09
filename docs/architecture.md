# Architecture

## The one idea

A health assistant is only as good as the joins it can make. "You slept badly"
and "you ran hard" are two facts; *"your resting heart rate is still +4 bpm two
days after that 6 km tempo run, and the night after it your REM collapsed to
5%"* is a finding. Findings need a graph, and a graph needs a schema.

Bodymesh is that schema with a CLI bolted on. The assistant is downstream of it,
never inside it.

```
 ingest            normalise          derive              present
┌─────────┐      ┌──────────┐      ┌────────────┐      ┌───────────┐
│ Garmin  │      │  daily   │      │  metrics   │      │   brief   │
│ Photos  │ ───▶ │ activities│ ───▶ │  insights  │ ───▶ │  (markdown)│
│ Notes   │      │  meals   │      │  baselines │      │   + --json │
│ Weigh-in│      │  body    │      │  signals   │      └─────┬─────┘
└─────────┘      └──────────┘      └────────────┘            │
                                                            ▼
                                                    chat gateway / cron
```

## Why a CLI instead of MCP

MCP is a transport, not an architecture. What an agent actually needs is a
**stable, typed, side-effect-free-ish contract**, and a CLI that prints JSON is
that contract with fewer moving parts:

- works from any language, any framework, `cron`, `ssh`, or a shell script;
- the same command a human debugs by hand is the one the model calls;
- no daemon to keep alive on a 1.6 GB VPS;
- failure modes are exit codes, not dangling transports.

If you *do* want MCP, the adapter is ~40 lines: wrap each `bm ... --json`
invocation in a tool definition. The architecture does not change.

## Layers inside `bodymesh.db`

| Layer | Tables | Mutability | Purpose |
|---|---|---|---|
| Raw | `daily`, `activities`, `body`, `meals`, `notes` | upserted, idempotent | facts with a timestamp |
| Derived | `insights` | recomputed per date | findings with `evidence` JSON |
| Output | `briefs` | append per date | what was actually said, for audit |

Idempotency matters: `bm sync --days 7` can run hourly. It upserts on
`(date)`, `(activity id)`, and de-duplicates weigh-ins on `(date, weight)`.

## The statistics, honestly

From `insights.py`:

**Drift** — 7-day window vs 28-day baseline, e.g.

```
rhr      r7 = 52.0   r28 = 49.4   → +2.6 bpm
sleep    s7 = 6.6 h  s28 = 7.5 h  → -0.9 h/night
```

**Coupling** — Pearson `r` on one-day-lagged pairs, gated by `n ≥ 10` and
`|r| ≥ 0.35`. The gate exists because with `n = 6` almost anything correlates.

**The energy-balance cross-check** — the most useful single computation here:

```
observed   = least-squares slope of weigh-ins     → -0.21 kg/week
expected   = mean(intake − burn) × 7 / 7700       → -0.79 kg/week
gap        = +0.58 kg/week
```

A gap that large almost always means *incomplete logging*, not a broken
metabolism. Reporting both numbers is what makes the log self-auditing. Note the
7,700 kcal/kg fat constant is a textbook approximation; water and glycogen swing
the scale far more than fat does week to week, which is why the regression runs
over every weigh-in rather than first-vs-last.

## Deliberate non-features

- **No ML model.** 30–90 days of one person's data is not a training set.
- **No cloud sync.** The database is a file you own.
- **No advice in the brief.** Facts in the brief, judgement in the message.
- **No automatic weight correction of Garmin data.** If Garmin says 88.42 kg,
  that is what gets stored, even if it looks like a rounding artefact.

## Extension points

| Want | Do |
|---|---|
| New source (Withings, Oura, Apple Health) | add `ingest/<name>.py` returning `daily` rows, wire into `sync` |
| New metric | add a column to `daily`, add the field to `garmin.daily_row` |
| New finding | one function in `insights.py` returning a `_finding(...)` |
| New output channel | anything that reads `bm daily --json` |
| Different model | `BODYMESH_LLM_BASE` + `BODYMESH_VISION_MODEL` |
