# Homelab service inventory

Derived from `catalog.yaml` (Proxmox node "Proxmox"). The catalog is the
source of truth the agent reads at runtime; this page is the human-readable
companion for the docs search. Keep them in sync when you change the catalog.

## Criticality levels

- **critical** — page anytime, day or night (router, Home Assistant, network).
- **high** — alerts too, but delivered silently from 23:00 to 07:00; restart attempts allowed (with approval).
- **medium** — daily digest only.
- **lab** — no alerts unless explicitly asked (dev / experiments).

## Restart policy

- **ask** — agent must request operator approval (default, safest).
- **auto** — reserved: not implemented, every restart still asks for approval.
- **never** — enforced in code: the restart tools refuse this guest even if a restart is approved.

## Guests

### OPNSense (vmid 105, qemu) — critical, restart: never
Router / firewall / DHCP / VLAN gateway. Losing it = no internet, so the
agent never restarts it. Web UI at https://router.lan (self-signed TLS).
Backups should be no older than 24h.

### HomeAssistant (vmid 100, qemu) — critical, restart: ask
Home Assistant Core — automations, energy, presence. API at
http://homeassistant.lan:8123/api/ (auth via `HA_TOKEN`). Backups ≤ 24h.

### Hermes (vmid 101, lxc) — high, restart: ask
The Hermes agent platform: gateway + dashboards. Reachability
probes the unauthenticated gateway health JSON at
http://hermes.lan:9119/api/status (`gateway_running` field).

### Debian13 (vmid 103, qemu) — high, restart: ask
Docker host running ~27 containers (live count via Portainer): AdGuard DNS (the LAN's primary
resolver), Immich (photos), Vaultwarden (passwords), Jellyfin (media),
openwebui, n8n, Linkwarden, Firefly III, Duplicati, Syncthing, Portainer,
Watchtower, plus supporting Postgres / MariaDB / Redis. On VLAN 60
(docker.lan), 6 GB RAM, 8 cores. AdGuard DNS reachability is checked on
TCP 53.

### sentinel (vmid 106, lxc) — high, restart: ask
HomelabSentinel itself — the agent + Telegram bot. If it dies, there is no
agent. This is the LXC these docs and the code run on.

(VMs 102 and 104 were destroyed; they were
removed from the catalog on 2026-09-26.)

## Proxmox host

- Node `Proxmox`, SSH `root@pve.lan:22` (key-based), used for
  `smartctl` SMART scans.
- Disks monitored: `/dev/sda`, `/dev/nvme0n1`.
- Backup storage: `general-storage` (as it appears under Datacenter →
  Storage). NOTE: backups currently live on the same Proxmox host they back
  up — moving them offsite (rclone to S3/Backblaze, or PBS to a NAS) is an
  open task.

## Energy monitoring

No whole-house meter — the energy assistant sums watched Tuya smart plugs
(LocalTuya `*_electricity` counters) as the total. Flat tariff: 30.40 ¢/kWh
(EUR). Watched plugs: Refrigerator, Dishwasher, Washing machine, two laptops, TV stand, Decor lights, Backlight, Ryzen 7 PC. The
counters reset often; the delta logic only sums upward steps, so a reset
loses at most one reading. The washing machine currently has no reporting
sensor in HA and shows "-".

The assistant sums these plugs **directly** — it does not read the HA Energy
dashboard's grid group (`sensor.tracked_energy_total`). Keep the two plug
lists in sync if you need them to agree.

## ESPHome devices

Sentinel watches ESPHome nodes through Home Assistant (the IoT VLAN is not routable from the Sentinel LXC). A node is online
while any of its entities has a real state, and offline once all of them
have been `unavailable` for 3 minutes. Watched (catalog `esphome:`,
`monitor: true`): BathRoom Monitor, BedRoom Monitoring Display, EPaper, Hall
Clock, Kitchen Display, LivingRoom Monitor, Office Monitor, S3 Dashboard.
Parked (`monitor: false`, never alert): TV Remote, Plant Moisture (a
deep-sleep MQTT-only node whose leftover HA entry always reads available).
To start watching a parked node, set `monitor: true` for it in
`catalog.yaml`. Ask the bot "are my ESP devices online?" for the live list.
