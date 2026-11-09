# Physarum 🧫

> **A living data mesh for your body.**
> Wearables give you numbers. Physarum turns them into a network you can ask questions of — and a daily brief that actually says something.

Named after *Physarum polycephalum*, the slime mold that solves mazes and rebuilds the Tokyo rail network out of nothing but food sources and hunger. That is the whole idea here: scattered points of evidence (a run, a bad night, a bowl of noodles) get connected into a network that finds the shortest path to a better body.

```
        ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
        │   Garmin     │   │    Meals     │   │    Notes     │
        │ rhr/hrv/sleep│   │ photo → kcal │   │ how you felt │
        └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
               └──────────┬───────┴──────────────────┘
                          ▼
                 ┌─────────────────┐
                 │    physarum.db  │  SQLite · one file · no server
                 │ raw → daily →   │
                 │ derived findings│
                 └────────┬────────┘
                          ▼
              ┌───────────────────────┐
              │  phy (CLI, --json)    │  the only tool surface
              └───────┬───────────────┘
                      ▼
        ┌───────────────────────────────┐
        │ chat agent (any framework)    │  09:00 brief · photo logging
        └───────────────────────────────┘
```

## Why not just an agent + MCP?

Because the interesting part is not the chat window.

- **Numbers must be computed, not narrated.** Every figure in the daily brief comes from `metrics.py` and can be reproduced byte-for-byte. The model only writes the coaching sentence on top. Ask it "why did you say my RHR was up?" and you get a SQL query back, not a vibe.
- **The agent must be replaceable.** Physarum is a CLI with a stable `--json` contract. Swap DeepSeek for anything else, swap OpenClaw for a cron job plus `curl`, and the data plane keeps working.
- **Findings need evidence.** Each stored insight carries `r`, `n`, and the exact pairing rule used, so a correlation can be dismissed as easily as it was raised.
- **One file beats a stack.** `physarum.db` is a portable graph of your body. `scp` it, `sqlite3` it, back it up with `cp`.

## Quick start

```bash
git clone https://github.com/arczhi/physarum.git
cd physarum
uv venv .venv && source .venv/bin/activate
uv pip install -e .

export DEEPSEEK_API_KEY=sk-...        # used for food photos only
phy init
phy doctor                            # environment self-check
phy sync --days 14                    # pull Garmin (needs ~/.garminconnect tokens)
phy daily                             # yesterday: sync + findings + brief
```

Sample output:

```
## 数据快照 · 2026-03-14
**概览** 步数 11,204 · 活动消耗 690 kcal · 强度 58 min · 压力 31 · 静息 51 bpm
**睡眠** 6.4 h · 得分 71（FAIR）· 深睡 15% · REM 12% · 夜间心率 57
**HRV** 92 ms · 状态 BALANCED · 个人平衡区间 78–101
**训练**
- running · 6.03 km · 43:30 · 7'12"/km · avg 172 bpm · max 184 · load 84 · TE 3.9 · cadence 166 · GCT 284 ms
**饮食** 3 餐 · 2,180 kcal · 蛋白 119 g · 碳水 238 g · 脂肪 72 g
**体重** 88.40 kg（2026-03-14）· 趋势 -0.24 kg/周
**能量** 摄入 2,180 vs 消耗 2,960 → -780 kcal/天（近 14 天，记录 9 天）
**本周负荷** 2 次 · 8.61 km · 65 min · load 168
### 身体信号
- 🟡 睡眠 6.4 小时，略短于目标 8 小时
- 🟡 REM 睡眠仅占 12%（健康区间 21–31%），常见于睡太短、酒精或交感神经未放松
### 规律与趋势
- 睡眠时长 → 次日静息心率：负相关 r=-0.46（n=18）
- 体重趋势 -0.24 kg/周（实测），-0.71 kg/周（按热量缺口推算）
**参考目标** 蛋白 141–194 g/天 · 睡眠 8 h/天
```


## Command surface

Everything is `phy <verb>`; add `--json` to any command for machine output.

| Command | What it does |
|---|---|
| `phy init` | create the SQLite database |
| `phy sync --days 14` | pull Garmin: daily wellness, activities, weigh-ins |
| `phy daily` | sync + recompute findings + render the brief (the cron job) |
| `phy brief --date 2026-11-08` | re-render any day's brief |
| `phy status` | compact snapshot for "how am I doing?" |
| `phy meal add --image lunch.jpg` | photo → structured nutrition |
| `phy meal add -d "牛肉面" --kcal 780 --protein 32` | manual meal |
| `phy meal list` | today's meals |
| `phy body add --weight 96.3 --fat 24.1` | weigh-in |
| `phy note add "膝盖有点紧"` | free-text note |
| `phy trend --metric rhr --days 30` | ASCII trend |
| `phy insights --days 30` | stored findings with evidence |
| `phy doctor` | env/db/token self-check |

## Data model

Four layers, one file.

1. **`daily`** — one wide row per day: RHR, HRV + personal baseline, sleep stages, stress, body battery, steps, calories, training load, VO2max, weight.
2. **`activities`**, **`meals`**, **`body`**, **`notes`** — event tables (what happened, when).
3. **`insights`** — derived findings, each with a `code`, `severity`, and JSON `evidence` (`r`, `n`, pairing rule). Rewritten per date, so they never rot.
4. **`briefs`** — the exact markdown that was generated, as an audit trail.

## What the pattern finder actually claims

Only two kinds of statements, both falsifiable:

**Drift** — a 7-day window against its own 28-day baseline: resting heart rate, HRV, sleep duration, protein density, weight rate, and a cross-check of *observed* weight change versus *energy-deficit-implied* weight change (7,700 kcal per kg of fat). A large gap between those two numbers is the single most useful signal you can get from a food log, because it means the log is incomplete — not that physics broke.

**Coupling** — Pearson `r` between daily series with a one-day lag (sleep → next-day RHR, training load → next-night sleep score, late meals → sleep quality). Reported only with `n ≥ 10` and `|r| ≥ 0.35`, always with the sample size attached.

There is no ML in here on purpose. With 30–90 days of data, a correlation with a visible `n` is more honest than a model with a hidden one.

## Honest limits

- **Garmin's burn number is a model, not a measurement.** Energy balance is labelled as such everywhere it appears. Treat ±10% as noise.
- **Food photos are estimates.** `confidence` is stored on every meal; a low value is a signal to you, not a rounding error to be hidden.
- **Wrist optical sensors lie about HRV under movement and sweat.** Post-workout stress values are inflated; the nightly ones are not. Compare trends, never single points.
- **ACWR is contested.** The `load_ratio` field is Garmin's model output; the literature on "optimal" ratios is genuinely split, so it never appears as a hard gate.
- **Nothing here is medical advice.** It is a record-keeping and pattern-surfacing tool.

## Scheduling

The brief is meant to arrive on its own:

```bash
# data refresh + brief generation, 08:50 local
50 8 * * *  cd /opt/physarum && /opt/physarum/.venv/bin/phy daily >> /var/log/physarum.log 2>&1
```

Then let whatever chat gateway you use read `phy daily` output at 09:00 (OpenClaw's cron, a `curl` to a webhook, or plain `sendmail`). See `docs/agent-integration.md`.

## Deploy

```bash
sudo bash scripts/install-vps.sh   # python + venv + systemd timer, no root daemons
```

Garmin OAuth tokens live outside the repo (`~/.garminconnect`), the model key lives in `/etc/physarum/physarum.env`, and the database lives in `/var/lib/physarum/`. `.gitignore` refuses all three.

## License

MIT. Your body, your data, your file.
