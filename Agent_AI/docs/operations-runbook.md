# HomelabSentinel — operations runbook

Quick-reference for operating the Sentinel agent. Sentinel runs as an
unprivileged Proxmox **LXC 106** ("sentinel"); the code lives flat in
`/opt/sentinel` (this is the live deployment, not a checkout — there is no
git remote here, so edit files in place).

## The bot

```bash
systemctl status sentinel-bot.service          # is it running?
journalctl -u sentinel-bot.service -f          # follow logs
systemctl restart sentinel-bot.service         # apply code changes
```

The bot is the long-running Telegram conversational agent
(`sentinel_bot.py`). On restart it sends a "✅ online" message to the
authorized chat — that ping is normal. Only `sentinel_bot.py` should poll
Telegram; do not run `agent_v5_approval.py` at the same time as the bot for
anything that asks approval, since both would fight for callback updates.

## Scheduled monitors (systemd timers)

```bash
systemctl list-timers 'sentinel-*'             # next/last run of each
systemctl start sentinel-<name>.service        # run one NOW
journalctl -u sentinel-<name>.service -n 50    # its last output
```

The five timers and what they do:

- `sentinel-reachability` — probes every catalogued endpoint (every ~5 min).
- `sentinel-docker` — Docker container health check via Portainer (every ~10 min).
- `sentinel-smart` — nightly SMART disk scan (~02:30).
- `sentinel-backups` — daily backup-freshness check (~09:00).
- `sentinel-energy` — daily energy digest sent to Telegram (~21:00).

Each monitor sends Telegram alerts only when something is wrong (energy
always sends its digest because it runs with `--digest`). They generate
their summaries on the local Gemma helper, not Claude.

## Pausing things

```bash
systemctl stop sentinel-reachability.timer     # pause one monitor
systemctl stop 'sentinel-*.timer'              # pause all monitors
systemctl stop sentinel-bot.service            # stop the bot
```

## Audit log

Every approval request, decision, execution, and (since the token-economy
work) every Claude call's token usage is appended here:

```bash
tail -f /opt/sentinel/audit.log | jq .
tail -f /opt/sentinel/audit.log | jq 'select(.event=="llm_usage")'   # cache hits
```

## Docs / RAG

```bash
uv run python rag.py --ingest                  # rebuild the docs index
uv run python rag.py "your question"           # query the docs (Gemma answer)
```

## Models

- Reasoning + tool-calling agent → Claude Sonnet (Anthropic API). This is the
  only thing that spends money; it requires credits on the Anthropic account.
  If every query returns `❌ Internal error: BadRequestError`, check the
  credit balance at console.anthropic.com.
- Cheap single-shot summaries / classification / RAG generation → local Gemma
  via the llama-server in LXC 101 ("ollama"). See `models.py`.
