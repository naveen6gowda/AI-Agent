# Voice setup — talk to Sentinel through Alexa

Sentinel's voice path is Alexa-native, the same shape as controlling a light
("Alexa, switch off the passage light" → HA → Zigbee):

```
"Alexa, <phrase>"  →  Alexa Routine  →  HA automation/script
   →  POST http://sentinel.lan/voice  →  Sentinel runs the intent
   →  Echo speaks the answer (Alexa Media Player TTS)
```

Amazon does the speech-to-text, so there is no Whisper on this path. The
common commands (status / backups / energy / disks / who's home) run the
local monitors and summarize on Gemma — they cost **zero Claude tokens** and
work even when the Anthropic balance is empty.

## 1. Sentinel side (this LXC, sentinel.lan)

Set a shared token in `/opt/sentinel/.env`:

```
VOICE_SERVER_TOKEN=choose-a-long-random-string
VOICE_ALEXA_TARGET=alexa_media_echo_dot   # notify.<this> = your Echo
VOICE_SERVER_PORT=8099
```

Install and start the service:

```bash
sudo cp /opt/sentinel/systemd/sentinel-voice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-voice.service
curl -s localhost:<port>/health | jq .        # {"ok":true,"intents":[...]}
```

## 2. Home Assistant side

### a) REST command — `configuration.yaml`

```yaml
rest_command:
  sentinel_voice:
    url: "http://sentinel.lan/voice"
    method: POST
    headers:
      Authorization: !secret sentinel_voice_token   # put 'Bearer <token>' in secrets.yaml
    content_type: "application/json"
    payload: '{"intent": "{{ intent }}", "text": "{{ text | default('''') }}", "speak": true}'
```

### b) One script per spoken command — `scripts.yaml`

```yaml
sentinel_status:
  alias: Sentinel Status
  sequence:
    - action: rest_command.sentinel_voice
      data: { intent: "status" }
sentinel_backups:
  alias: Sentinel Backups
  sequence:
    - action: rest_command.sentinel_voice
      data: { intent: "backups" }
sentinel_energy:
  alias: Sentinel Energy
  sequence:
    - action: rest_command.sentinel_voice
      data: { intent: "energy" }
sentinel_disks:
  alias: Sentinel Disk Health
  sequence:
    - action: rest_command.sentinel_voice
      data: { intent: "disks" }
sentinel_presence:
  alias: Sentinel Who Is Home
  sequence:
    - action: rest_command.sentinel_voice
      data: { intent: "presence" }
sentinel_docker:
  alias: Sentinel Docker Containers
  sequence:
    - action: rest_command.sentinel_voice
      data: { intent: "docker" }
```

Reload scripts (Developer Tools → YAML → Scripts) after adding them.

### c) Trigger the scripts from Alexa — via `alexa_media_player`

You already have the HACS **Alexa Media Player** integration installed, which
is enough on its own — *no Nabu Casa, no Smart Home Skill, no Emulated Hue*.
The trick: when Alexa hears a wake-word + phrase on an Echo, that Echo's
`media_player.<echo>` entity in HA updates `last_called_summary` with the
transcribed phrase. We watch that attribute and dispatch to the matching
script.

Add this single automation to `automations.yaml`:

```yaml
- id: sentinel_voice_phrase_listener
  alias: Sentinel Voice Phrase Listener
  description: Dispatch sentinel_* scripts when Alexa hears a sentinel phrase.
  triggers:
    - trigger: state
      entity_id: media_player.echo_dot   # one entry per Echo
      attribute: last_called_summary
  conditions:
    - condition: template
      value_template: "{{ trigger.to_state.attributes.last_called_summary not in [none, '', 'stop', 'cancel'] }}"
  actions:
    - variables:
        phrase: "{{ (trigger.to_state.attributes.last_called_summary or '') | lower }}"
    - choose:
        - conditions:
            - condition: template
              value_template: "{{ 'system status' in phrase or 'homelab status' in phrase or phrase == 'status' }}"
          sequence: [{ action: script.sentinel_status }]
        - conditions:
            - condition: template
              value_template: "{{ 'backup' in phrase }}"
          sequence: [{ action: script.sentinel_backups }]
        - conditions:
            - condition: template
              value_template: "{{ 'energy' in phrase or 'power report' in phrase or 'power status' in phrase }}"
          sequence: [{ action: script.sentinel_energy }]
        - conditions:
            - condition: template
              value_template: "{{ 'disk health' in phrase or 'disk status' in phrase or phrase == 'disks' }}"
          sequence: [{ action: script.sentinel_disks }]
        - conditions:
            - condition: template
              value_template: "{{ 'who is home' in phrase or \"who's home\" in phrase or 'presence' in phrase }}"
          sequence: [{ action: script.sentinel_presence }]
        - conditions:
            - condition: template
              value_template: "{{ 'docker' in phrase or 'containers' in phrase or 'container status' in phrase }}"
          sequence: [{ action: script.sentinel_docker }]
  mode: parallel
  max: 5
```

Reload: Developer Tools -> YAML -> Automations (or call
`automation.reload`).

For multiple Echos, duplicate the `entity_id` line and the trigger
fires for whichever Echo heard the phrase.

### d) Suppress Alexa's "I don't understand" — Routines

The automation in (c) fires regardless of whether Alexa recognized the
phrase, because `last_called_summary` is set by Amazon's STT before any
intent matching. But without a Routine, the Echo will respond "Sorry, I
don't know that one" *before* Sentinel's answer arrives. To make Alexa
acknowledge politely instead, create one Routine per phrase in the Alexa
app:

| When you say          | Alexa says (action)          |
|-----------------------|-------------------------------|
| "system status"       | "On it"                       |
| "backup status"       | "Checking backups"            |
| "energy report"       | "Pulling energy"              |
| "disk health"         | "Checking disks"              |
| "who is home"         | "Looking"                     |
| "container status"    | "Checking containers"         |

Sentinel's answer plays a few seconds later through the same Echo.

> Note: this path depends on `media_player.<echo>.last_called_summary`,
> which the alexa_media_player integration updates from the Amazon cloud
> on the next poll after the wake-word fires (usually <2s). If the Echo
> is currently the **last_called** one (`last_called: true`), the
> transcribed phrase is reliable. Echos other than the last-called one
> won't have a fresh summary — that's why the trigger is scoped to the
> specific Echo entity.


## Available intents

`status` (a.k.a. health/reachability), `backups`, `energy` (power),
`disks` (smart), `presence` (home/who), `docker` (containers), and `ask`
(free-form → Claude, read-only).

## Free-form "ask anything"

Alexa Routines can only match fixed phrases, so arbitrary questions
("Alexa, ask Sentinel why is the dishwasher using so much power") need an
Alexa **custom skill** with an `AMAZON.SearchQuery` slot that POSTs
`{"intent":"ask","text":"<the spoken text>"}` to `/voice`. The server already
supports that intent — only the Amazon-side skill + a public HTTPS endpoint
are missing. That's the upgrade path when fixed commands aren't enough.

## Test without Alexa

```bash
TOKEN=choose-a-long-random-string
# run an intent but DON'T make the Echo talk:
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"intent":"status","speak":false}' localhost:<port>/voice | jq .
# make the Echo actually speak a test line:
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"Sentinel voice is online."}' localhost:<port>/speak | jq .
```
