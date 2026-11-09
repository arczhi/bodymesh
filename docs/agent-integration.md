# Wiring a chat assistant to Bodymesh

This is the **deployment that actually runs**, not a sketch. Every command below
was executed on a 2 vCPU / 1.6 GB Ubuntu 24.04 VPS, and the numbers quoted
(latency, RSS, timings) were measured there.

Two halves, deployed independently:

| Half | Job | Lives in |
|---|---|---|
| **Data** | 08:50 — pull Garmin, recompute findings, render the brief | `bm` + systemd timer |
| **Voice** | 09:00 — read the brief, talk to the human, accept meal photos | nanobot + a chat channel |

```
 08:50 systemd ──▶ bm daily ──▶ bodymesh.db ──▶ briefs.md (deterministic)
                                                │
 09:00 nanobot cron ──▶ agent turn ──▶ bm daily --no-sync --json
                              │                    │
                              ▼                    ▼
                     deepseek-flash        numbers + signals
                              │
                              ▼
                        WeChat (personal account, QR login)
```

The boundary that makes this cheap to change: **the assistant never computes a
number.** It shells out to `bm`, reads JSON, and narrates. Swap the assistant and
the data plane keeps working; swap the data plane and the assistant keeps working.

## Production layout

```
/opt/bodymesh   →  /data/bodymesh            code (git clone), .venv
                    ├── .env                 mode 600, gitignored: model key + paths
                    └── data/
                        ├── bodymesh.db      the mesh
                        ├── inbox/           chat attachments land here
                        └── .garminconnect/  OAuth tokens, mode 600
/data/nanobot
                    ├── .env                 mode 600: DEEPSEEK_API_KEY
                    ├── .venv/               nanobot-ai
                    └── home/                --home instance root
                        ├── config.json      providers, presets, channels, tools
                        ├── workspace/       AGENTS.md, SOUL.md, USER.md
                        ├── cron/jobs.json   managed by CronService, don't hand-edit
                        ├── sessions/        per-chat SQLite history
                        └── weixin-state/account.json
/usr/local/bin/bm        wrapper: sources .env, execs the venv binary
/usr/local/bin/nanobot   wrapper: sources .env, forces --home, uses `python -m`
```

## 1. Data half: 08:50 refresh

```ini
# /etc/systemd/system/bodymesh-sync.service
[Unit]
Description=Bodymesh: Garmin sync + findings + daily brief
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/data/bodymesh/.env
WorkingDirectory=/data/bodymesh
ExecStart=/data/bodymesh/.venv/bin/bm daily --json
StandardOutput=append:/data/bodymesh/logs/daily.json
StandardError=append:/data/bodymesh/logs/daily.err
```

```ini
# /etc/systemd/system/bodymesh-sync.timer
[Unit]
Description=Bodymesh daily refresh (08:50 Asia/Shanghai)

[Timer]
OnCalendar=*-*-* 08:50:00
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

`Persistent=true` matters on a VPS that can suspend: a missed 08:50 runs on boot
instead of silently skipping the day. Measured: a 12-day sync takes **~9 s**.

## 2. Voice half: nanobot

Why nanobot and not the heavier gateways: it is Python (same runtime as the data
plane), one `pip install`, ~45 dependencies, **213 MB RSS** for the gateway, and
it ships `weixin` / `wecom` / `feishu` / `telegram` / `email` channels plus a cron
scheduler. A TypeScript monorepo gateway with a bun/pnpm build chain does not fit
a 1.6 GB box — that was measured too, not assumed.

### 2.1 Install

```bash
python3 -m venv /data/nanobot/.venv
/data/nanobot/.venv/bin/pip install nanobot-ai   # 0.4.1 at the time of writing
/data/nanobot/.venv/bin/python -m nanobot --home /data/nanobot/home onboard
```

`onboard` (without `--wizard`) creates `config.json` and a workspace containing
`AGENTS.md`, `SOUL.md`, `USER.md`, `HEARTBEAT.md`, `memory/`, `skills/`.

### 2.2 Provider: DeepSeek

DeepSeek is OpenAI-compatible, so it is a `providers.<name>` entry plus a preset.
Keep the key in the environment and reference it — the config file then never
contains a secret.

```json
{
  "providers": {
    "deepseek": {
      "apiKey": "${DEEPSEEK_API_KEY}",
      "apiBase": "https://api.deepseek.com/v1"
    }
  },
  "modelPresets": {
    "flash": {
      "provider": "deepseek",
      "model": "deepseek-flash",
      "maxTokens": 8192,
      "contextWindowTokens": 1048576,
      "temperature": 0.3
    }
  },
  "agents": { "defaults": { "modelPreset": "flash" } }
}
```

Model IDs as served by `GET https://api.deepseek.com/models`:

| ID | Display name | Modalities | Use |
|---|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash | **text + image** | chat *and* food photos |
| `deepseek-v4-pro` | DeepSeek-V4-Pro | text only | reasoning-heavy turns |

There is no `deepseek-v4-flash`; the Flash id is `deepseek-flash`. Because Flash
accepts images, the same key serves both the conversation and
`bm meal add --image` — no second vision provider is needed.

Allow the shell tool to find `bm` and keep the workspace guard off (the data
plane writes to `/data/bodymesh/data`):

```json
{
  "tools": {
    "restrictToWorkspace": false,
    "exec": {
      "enable": true,
      "pathPrepend": "/usr/local/bin:/data/bodymesh/.venv/bin:/usr/bin:/bin",
      "maxRuntime": 300
    }
  }
}
```

### 2.3 WeChat, personal account

`nanobot channels login weixin` performs the QR handshake and, on confirmation,
writes `weixin-state/account.json`. The QR it prints is ASCII art, which is
useless over a chat bridge, so the production flow fetches the URL and renders a
PNG:

```python
# pseudo-python, mirrors nanobot/channels/weixin/runtime.py
ch.connect_open_client()                      # required: allocates the httpx client
qid, url = await ch.connect_fetch_qr_code(force=True)   # url is what the QR encodes
qrcode.make(url).save("/tmp/wx_qr.png")       # ship this PNG to the human
while True:                                   # ~3 s cadence, stop on token/expiry
    st = await ch.connect_poll_qr_code(base_url=ch.connect_base_url, qrcode_id=qid)
    if st.get("bot_token"):
        ch.connect_commit_account(token=st["bot_token"], base_url=st.get("baseurl"))
        break
```

A scan returns `{"status": "confirmed", "bot_token": "…"}`. The QR is valid for a
few minutes; regenerate with `force=True` rather than showing a stale image.

Then enable the channel:

```json
{
  "channels": {
    "weixin": {
      "enabled": true,
      "stateDir": "/data/nanobot/home/weixin-state",
      "allowFrom": ["<sender-id>@im.wechat"]
    }
  }
}
```

`allowFrom` is the important field — see trap #3.

### 2.4 The 09:00 brief

Create it through the service API, **not** by editing `jobs.json` (trap #2):

```python
from pathlib import Path
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule

chat = "<sender-id>@im.wechat"
svc = CronService(Path("/data/nanobot/home/workspace/cron/jobs.json"))

svc.add_job(
    name="bodymesh-morning-brief",
    schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
    message=(
        "早间简报。第一步执行 `bm daily --no-sync --json`。\n"
        "然后按 AGENTS.md 发一条中文简报：1) 一句话判断 2) 1-2 个数字 "
        "3) 今天做的一件事 4) 最多一个反问。不编造数字；正文≤120字。"
    ),
    deliver=True,
    channel="weixin",
    to=chat,
    session_key=f"weixin:{chat}",   # the bound session
    origin_channel="weixin",        # without these two, delivery is dropped
    origin_chat_id=chat,
    origin_metadata={},
)
```

The resulting payload shows `deliver: false, channel: null, to: null` and logs
`migrated legacy job … to session-bound payload`. **That is correct, not a bug** —
delivery is now driven by the session binding (`session_key` + `origin_*`).

### 2.5 Gateway as a system service

```ini
# /etc/systemd/system/nanobot-gateway.service
[Unit]
Description=nanobot gateway (bodymesh health assistant)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/local/bin/nanobot gateway --foreground
Restart=always
RestartSec=5
WorkingDirectory=/data/nanobot/home
StandardOutput=append:/data/nanobot/gateway.log
StandardError=append:/data/nanobot/gateway.err
```

`nanobot gateway status` reports the pid, port (18790), client count and log path;
`GET /health` on `127.0.0.1:18790` is available for monitoring.

### 2.6 Workspace: who the assistant is

Three files decide whether the assistant is useful or a liability:

- **`AGENTS.md`** — the tool contract. Bodymesh's repo already ships one; the
  deployment appends the absolute paths (`/data/bodymesh/data/inbox`, `bm`,
  the 09:00 routine, the "no invented numbers" rules).
- **`SOUL.md`** — tone: short, conclusion first, one action per message, say
  "I don't know" when `n` is small, never practise medicine.
- **`USER.md`** — the human: language, timezone, body numbers, the habit of
  photographing meals, and *why* they are doing this.

These are assembled into the model context, so they are worth writing carefully —
they cost a few hundred tokens and buy most of the behavioural quality.

## 3. The three traps (all hit, all fixed)

### Trap #1 — the console script hangs, `python -m` does not

**Symptom:** every `nanobot <verb>` takes >15 s and sometimes never returns;
killed processes leave CPU-bound strays (load 4.2 on 2 vCPU).

**Cause:** the installed entry point performs a release/trust check
(`release_feed`, `release_trust`, sigstore + GitHub) before dispatching. On a
Chinese VPS that call blocks. `python -m nanobot` skips it.

**Fix:** never call the console script directly:

```bash
#!/bin/sh
set -a; . /data/nanobot/.env; set +a
exec /data/nanobot/.venv/bin/python -m nanobot --home /data/nanobot/home "$@"
```

Measured: 15 s+ → **1.35 s**. Always wrap agent invocations in `timeout` so a bad
network day cannot leave a spinner behind.

### Trap #2 — hand-edited `cron/jobs.json` loses its delivery binding

**Symptom:** the job fires, the agent produces a beautiful brief, and nothing
arrives in chat. The stored payload reads `deliver: false`.

**Cause:** the gateway loads `jobs.json` into memory, normalises jobs, and writes
them back. A hand-written payload has no session binding to survive that.

**Fix:** create jobs with `CronService.add_job(...)` and pass `session_key`,
`origin_channel`, `origin_chat_id` (§2.4). Stop the gateway before any offline
maintenance, and verify by *running the job*, not by reading the file.

### Trap #3 — the pairing code is a dead end without the WebUI

**Symptom:** the first message from a new chat returns
`Your pairing code is: XXXX-XXXX … approve it from an already paired chat`. There
is no already-paired chat, and no WebUI is exposed on the VPS.

**Cause:** channels default to a pairing gate; the code needs an approver.

**Fix:** whitelist the sender instead of pairing:

```json
{ "channels": { "weixin": { "allowFrom": ["<sender-id>@im.wechat"] } } }
```

The sender id is in the gateway log on that first inbound message
(`Generated pairing code … for <sender-id>`). Restart the gateway and the gate is
gone. The stale `pairing.json` entry is harmless.

## 4. Verifying each link in a minute

```bash
# 1 data plane
bm doctor && bm status

# 2 model reachability from this host (bypasses everything)
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' https://api.deepseek.com/models \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY"

# 3 the tool loop, without any chat channel
nanobot agent -m "执行 bm status --json，用一句话告诉我静息心率，不要编数字"

# 4 channel is connected
nanobot gateway status && nanobot channels status | grep -i wechat

# 5 scheduled delivery, rehearsed instead of waited for
#    add a one-shot job with kind="at", at_ms=now+30s, same binding, then:
grep -E "executing job|run=<name>|Channel delivery" /data/nanobot/gateway.err | tail -5
```

Verification #5 matters most: a `cron` schedule has its `nextRunAtMs` recomputed
on load, so you cannot force it by editing the timestamp — rehearse with an `at`
job, confirm `Channel delivery … status=sent`, then let the real 09:00 job run.

## 5. Ops notes

| Thing | Value |
|---|---|
| Gateway RSS | ~213 MB (leaves ~800 MB free on a 1.6 GB box) |
| A brief turn (flash) | 8–12 s, incl. one `bm daily` call |
| A two-photo meal turn | ~60 s (image upload + estimation) |
| Chat history | `home/sessions/<hash>/sessions.sqlite3` |
| Cron store | `home/cron/jobs.json` (service-owned) |
| Gateway logs | `gateway.log` (startup summary) / `gateway.err` (per-turn lines) |
| Health | `curl 127.0.0.1:18790/health` |

Restart after editing `config.json` or the workspace docs — the gateway caches
both:

```bash
systemctl restart nanobot-gateway
```

## 6. Alternatives, briefly

| Option | Verdict for this box |
|---|---|
| **nanobot** | ✅ chosen: Python, 1 pip install, 213 MB, built-in weixin/wecom/feishu, cron |
| OpenClaw | ❌ 13 GB TS monorepo + bun/pnpm build; `npm i -g` crawls and the Docker build needs more RAM than available |
| Letta / Agno / smolagents / pydantic-ai | fine libraries, but no chat channels — you would rebuild the transport, which is the genuinely hard part |
| Cron + webhook, no agent | legitimate! `bm daily --json | jq -r .markdown | curl -d @- …` — cheaper, dumber, more predictable |

## 7. Security notes

- Secrets live in `/data/bodymesh/.env`, `/data/nanobot/.env` (mode 600) and
  `data/.garminconnect/` (mode 600) — all outside git; `.gitignore` refuses them.
- `tools.restrictToWorkspace: false` is required because the data plane lives
  outside the workspace. If you ever expose this agent to more than one person,
  flip it on and add the data directory as an explicit read-write bind.
- The gateway binds `127.0.0.1` only; do not expose 18790 or the WebUI without a
  reverse proxy and auth.
- A WeChat personal account integration is unofficial. Prefer `wecom` (official
  API) for anything you would be sad to lose.
