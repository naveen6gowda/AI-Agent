# Homelab service inventory

Derived from `catalog.yaml` (Proxmox node "Proxmox"). The catalog is the
source of truth the agent reads at runtime; this page is the human-readable
companion for the docs search. Keep them in sync when you change the catalog.

## Criticality levels

- **critical** — page anytime, day or night (router, Home Assistant, network).
- **high** — alert during waking hours; restart attempts allowed.
- **medium** — daily digest only.
- **lab** — no alerts unless explicitly asked (dev / experiments).

## Restart policy

- **ask** — agent must request operator approval (default, safest).
- **auto** — agent may restart without asking (use sparingly).
- **never** — agent never restarts; it suggests manual action only.

## Guests

### OPNSense (vmid 105, qemu) — critical, restart: never
Router / firewall / DHCP / VLAN gateway. Losing it = no internet, so the
agent never restarts it. Web UI at https://router.lan (self-signed TLS).
Backups should be no older than 24h.

### HomeAssistant (vmid 100, qemu) — critical, restart: ask
Home Assistant Core — automations, energy, presence. API at
http://homeassistant.lan/api/ (auth via `HA_TOKEN`). Backups ≤ 24h.

### ollama (vmid 101, lxc) — high, restart: ask
llama.cpp server backing `helper_llm()` — the local Gemma the monitors and
RAG generation use. OpenAI-compatible API at http://llm.lan/v1
(auth via `LLAMACPP_API_KEY`). It serves a chat model only (no embeddings).

### Debian13 (vmid 103, qemu) — high, restart: ask
Docker host running ~27 containers (live count via Portainer): AdGuard DNS (the LAN's primary
resolver), Immich (photos), Vaultwarden (passwords), Jellyfin (media),
openwebui, n8n, Linkwarden, Firefly III, Duplicati, Syncthing, Portainer,
Watchtower, plus supporting Postgres / MariaDB / Redis. On VLAN 60
(docker.lan), 6 GB RAM, 8 cores. AdGuard DNS reachability is checked on
the DNS port.

### sentinel (vmid 106, lxc) — high, restart: ask
HomelabSentinel itself — the agent + Telegram bot. If it dies, there is no
agent. This is the LXC these docs and the code run on.

### Windows10 (vmid 102, qemu) — lab, restart: never
Desktop VM, started on demand. No backup urgency.

### HACK-SEQUOIA (vmid 104, qemu) — lab, restart: never
Pentest / sandbox VM. No backup urgency.

## Proxmox host

- Node `Proxmox`, SSH `root@pve.lan` (key-based), used for
  `smartctl` SMART scans.
- Disks monitored: `/dev/sda`, `/dev/nvme0n1`.
- Backup storage: `general-storage` (as it appears under Datacenter →
  Storage). NOTE: backups currently live on the same Proxmox host they back
  up — moving them offsite (rclone to S3/Backblaze, or PBS to a NAS) is an
  open task.

## Energy monitoring

No whole-house meter — the energy assistant sums watched Tuya smart plugs as
the total. Flat tariff: 30.40 ¢/kWh (EUR). Watched plugs: Refrigerator,
Dishwasher, Washing machine, Laptop A, Laptop B, TV stand, Decor
lights, Backlight. Caveat: the `*_total_energy` sensors reset ~once/day, so a
24h window that straddles a reset under-reports; switch to HA Utility Meter
helpers for billing-grade accuracy.

The assistant sums these 8 plugs **directly** — it does not read the HA Energy
dashboard's grid group (`sensor.tracked_energy_total`). If that group watches a
different plug set (e.g. it includes the omitted Ryzen 7 PC), the Telegram
energy digest total will differ from the HA Energy dashboard. Keep the two plug
lists in sync if you need them to agree.
