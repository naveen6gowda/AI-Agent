# HomelabSentinel — operations runbook

Quick reference for operating Sentinel (rewritten 2026-09-28; the older
version still described the retired Claude/Gemma setup). Sentinel runs in
Proxmox **LXC 106** ("sentinel", sentinel.lan). The code lives in
`/opt/sentinel`, which is a git repository: `main` is mirrored nightly to
the private GitHub repo `homelab-sentinel` by `sentinel-gitmirror` (04:15,
after ruff and pytest pass).

## Long-running services

| Unit | Port | What it is |
|------|------|-----------|
| `sentinel-bot` | — | Telegram chat agent + approval buttons (the ONLY Telegram poller) |
| `sentinel-mcp` | 8765 | MCP server: the same tools for Claude Code / Hermes, bearer token |
| `sentinel-voice` | 8099 | HA → agent → Alexa bridge, bearer token |
| `sentinel-finance` | 8098 | HA phone notification → Firefly III, bearer token |
| haproxy | 9099 | the LLM front door: Mac LM Studio on the LAN, Tailscale as backup |

```bash
systemctl status sentinel-bot                  # any unit
journalctl -u sentinel-bot -f                  # follow logs
systemctl restart sentinel-bot                 # apply code changes
```

The bot posts a silent "✅ online" message when it starts; the 03:30
maintenance restarts it every night, so one appears daily — that is normal.
Do not run `agent_v5_approval.py` by hand while the bot runs: both would
poll Telegram and fight over the approval taps.

## Scheduled monitors (systemd timers)

```bash
systemctl list-timers 'sentinel-*'             # next/last run of each
systemctl start sentinel-<name>.service        # run one NOW
journalctl -u sentinel-<name>.service -n 50    # its last output
```

| Timer | When | What |
|-------|------|------|
| `sentinel-esphome` | every 2 min | ESPHome nodes online/offline via HA (catalog `esphome:`) |
| `sentinel-reachability` | every 5 min | probes every catalogued endpoint |
| `sentinel-docker` | every 10 min | containers on Debian13 via Portainer + restart cards |
| `sentinel-guests` | every 15 min | every Proxmox VM/LXC: running? real memory? |
| `sentinel-db-train` | Mon–Fri 06–09, every 5 min | S2 / bus 700 delays → Alexa |
| `sentinel-speedtest` | every 4 h | internet speed vs threshold |
| `sentinel-smart` | 02:30 | SMART disk scan on the Proxmox host |
| `sentinel-maintenance` | 03:30 | checkpoint prune + VACUUM (restarts the bot) |
| `sentinel-gitmirror` | 04:15 | commit + lint + test + push to GitHub |
| `sentinel-backups` | 09:00 | backup freshness per catalogued guest |
| `sentinel-energy` | 21:00 | daily energy digest to Telegram |

## How alerts behave

- **Reachability, guests, docker** alert on the CHANGE: one message when
  something breaks, one when it recovers, a reminder every 6 h while it
  stays broken (`catalog.yaml` → `alerting:`). Reachability waits for a
  second failed probe (~5 min) so a single blip stays quiet.
- **critical** services page any time with sound; a message with only
  **high** news is delivered silently from 23:00 to 07:00.
- **ESPHome**: one message when a watched node goes offline (after 3 min)
  and one when it is back. Parked nodes (`monitor: false`) never alert.
- **Backups / SMART / speedtest** run rarely and alert on each run that
  finds a problem.
- **"🛠 Sentinel monitor broke"** means the CHECK itself failed (exit 1 →
  `OnFailure=` pager), not that the watched thing is down.

## Pausing things

```bash
systemctl stop sentinel-reachability.timer     # pause one monitor
systemctl stop 'sentinel-*.timer'              # pause all monitors
systemctl stop sentinel-bot                    # stop the bot
```

## State and logs (all under /opt/sentinel/var/, never committed)

```bash
tail -f /opt/sentinel/var/audit.log | jq .     # approvals, restarts, tool calls
ls /opt/sentinel/var/alerts/                   # open incidents per monitor
cat /opt/sentinel/var/esphome_state.json       # last known ESP node states
cat /opt/sentinel/var/active_model.txt         # the model /model selected
```

## Models

One local model serves everything: LM Studio on the Mac, reached through
the haproxy front door on this LXC (`MLX_BASE_URL=http://sentinel.lan:9099/v1`).
The active model id is `var/active_model.txt`, switched live with the
Telegram `/model` command (it probes the model before switching). If the Mac
is asleep, chat answers "LLM server unreachable"; the monitors keep working
because they detect problems without the LLM — only their summaries degrade
to a plain-text fallback.

## Docs / RAG

```bash
uv run python rag.py --ingest                  # rebuild the docs index
uv run python rag.py "your question"           # query the docs
```

The nightly maintenance also re-ingests `docs/` (including the
`docs/memory/` notes it writes for pruned conversations).
