# Wiring a chat assistant to Physarum

Two halves: **data** (a cron job that keeps the database warm) and **voice** (a
chat agent that reads `phy` and talks to you). They fail independently, which is
the point.

## 1. Data half: the 08:50 refresh

```bash
# /etc/systemd/system/physarum-sync.service
[Unit]
Description=Physarum Garmin sync + brief
After=network-online.target

[Service]
Type=oneshot
User=physarum
EnvironmentFile=/etc/physarum/physarum.env
ExecStart=/opt/physarum/.venv/bin/phy daily --json
StandardOutput=append:/var/log/physarum/daily.json
StandardError=append:/var/log/physarum/daily.err
```

```bash
# /etc/systemd/system/physarum-sync.timer
[Unit]
Description=Physarum daily refresh (08:50 local)

[Timer]
OnCalendar=*-*-* 08:50:00
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` matters: if the VPS was asleep at 08:50, the job runs on boot
instead of silently skipping a day.

## 2. Voice half: any agent that can run a command

The contract is one line: **run `phy`, read JSON, narrate.** A cron entry with a
small script is a perfectly legitimate "agent".

### OpenClaw (WeChat / WhatsApp / Telegram / …)

OpenClaw is a multi-channel gateway with a built-in scheduler. Physarum plugs in
as a shell tool plus an `AGENTS.md`.

**Provider** — DeepSeek is OpenAI-compatible, so declare it as a custom provider
in `~/.openclaw/openclaw.json5`:

```json5
{
  agents: { defaults: { model: { primary: "deepseek/deepseek-v4-pro" } } },
  models: {
    mode: "merge",
    providers: {
      deepseek: {
        baseUrl: "https://api.deepseek.com",
        apiKey: "${DEEPSEEK_API_KEY}",
        api: "openai-completions",
        models: [
          { id: "deepseek-v4-pro", name: "DeepSeek V4 Pro" },
          // image input — this is what reads your lunch
          { id: "deepseek-flash", name: "DeepSeek V4.1 Flash", input: ["text", "image"] }
        ]
      }
    }
  }
}
```

**Morning brief** — 09:00 local, delivered into the chat:

```bash
openclaw cron add \
  --name "physarum-morning-brief" \
  --cron "0 9 * * *" \
  --tz "Asia/Shanghai" \
  --session isolated \
  --announce \
  --channel weixin \
  --to "<your-weixin-id>" \
  --message "Run: /opt/physarum/.venv/bin/phy daily --json
Read the markdown field and the signals array. Then write a short Chinese
message: 1) one-sentence verdict on yesterday, 2) the 1-2 numbers that matter,
3) what to do today (concrete), 4) at most one question back.
Follow AGENTS.md in /opt/physarum. Never invent a number."
```

**Agent instructions** — point the agent's working directory at the repo, or copy
`AGENTS.md` into the agent's instructions file. That file is the difference
between "the model knows it has a database" and "the model knows how not to lie
about it".

### Anything else

- **Plain cron + webhook**: `phy daily --json | jq -r .markdown | curl -d @- ...`
- **Email**: same, piped to `msmtp`.
- **A human**: `phy daily` in a terminal. It is designed to be readable.

## 3. Interactive logging

The chat agent needs three things to do photo logging well:

1. The channel must save attachments to disk (WeChat plugins do; keep the path
   stable, e.g. `$PHYSARUM_INBOX`).
2. `phy meal add --image <path>` must be reachable by the agent's shell tool.
3. `AGENTS.md` must tell it to ask one clarifying question when
   `confidence < 0.5`.

The estimator call goes to `PHYSARUM_VISION_MODEL` at `PHYSARUM_LLM_BASE` with a
strict JSON schema, so a photo never depends on the chat model being
multimodal — the same model family can be used for both, or you can point
`PHYSARUM_LLM_BASE` at anything OpenAI-compatible.

## 4. Health checks

```bash
phy doctor                    # tokens, key, counts, latest day
systemctl list-timers physarum-sync
tail -n 20 /var/log/physarum/daily.err
```

If `doctor` says `garmin tokens MISSING`, re-run the Garmin device-flow login on
a machine with a browser and copy `~/.garminconnect/` over. Tokens are
credentials: `chmod 700` the directory, never commit it.
